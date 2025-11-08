from fastapi import FastAPI, HTTPException, status, Body, Depends
from typing import List
from datetime import datetime
import random
import httpx
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument
from pydantic import ValidationError

from functions.models import Trip, TripCreate, TripStatus, AcceptTripRequest, MockDriver
from functions.db import connect_to_mongo, close_mongo_connection, get_trip_collection
from functions.config import (
    DRIVER_SERVICE_URL, PAYMENT_SERVICE_URL, RIDER_SERVICE_URL, CANCELLATION_FEE
)
from motor.motor_asyncio import AsyncIOMotorCollection

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    await connect_to_mongo()
    try:
        trips_coll = get_trip_collection()
        await trips_coll.create_index("trip_id", unique=True)
        try:
            db = trips_coll.database
            counters = db["counters"]
            pipeline = [
                {"$sort": {"trip_id": -1}},
                {"$limit": 1}
            ]
            docs = await trips_coll.aggregate(pipeline).to_list(length=1)
            max_trip = docs[0].get("trip_id", 0) if docs else 0
            start_seq = int(max_trip) + 1

            current = await counters.find_one({"_id": "trip_id"})
            if current is None:
                await counters.insert_one({"_id": "trip_id", "seq": start_seq})
            else:
                try:
                    curr_seq = int(current.get("seq", 0))
                except (ValueError, TypeError):
                    curr_seq = 0

                if curr_seq < start_seq:
                    await counters.update_one({"_id": "trip_id"}, {"$set": {"seq": start_seq}})

        except Exception:
            pass
    except Exception:
        pass

@app.on_event("shutdown")
async def shutdown_event():
    await close_mongo_connection()

def get_trips_collection() -> AsyncIOMotorCollection:
    return get_trip_collection()

async def get_next_trip_id(collection: AsyncIOMotorCollection) -> int:
    db = collection.database
    counters = db["counters"]
    result = await counters.find_one_and_update(
        {"_id": "trip_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    if not result:
        pipeline = [
            {"$sort": {"trip_id": -1}},
            {"$limit": 1}
        ]
        docs = await collection.aggregate(pipeline).to_list(length=1)
        if docs:
            try:
                last_id = int(docs[0].get("trip_id", 0))
                return last_id + 1
            except (ValueError, TypeError):
                return 1
        return 1

    seq = result.get("seq")
    try:
        return int(seq)
    except (ValueError, TypeError):
        return 1

@app.post("/v1/trips", response_model=Trip, status_code=status.HTTP_201_CREATED)
async def request_trip(
    trip_request: TripCreate,
    trips_coll: AsyncIOMotorCollection = Depends(get_trips_collection)
):

    rider_url = f"{RIDER_SERVICE_URL}/riders/{trip_request.rider_id}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(rider_url)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Rider not found")
        raise HTTPException(status_code=503, detail="Rider service is unavailable")
    except Exception:
        raise HTTPException(status_code=503, detail="Error connecting to Rider service")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            mock_distance = round(random.uniform(3.0, 25.0), 2)
            mock_eta_minutes = int(mock_distance * 2.5)

            mock_base_fare = round(mock_distance * 14.5, 2)
            mock_surge = random.choice([1.0, 1.2, 1.5])
            mock_total_fare = round(mock_base_fare * mock_surge, 2)
            
            new_trip_id = await get_next_trip_id(trips_coll)

            new_trip = Trip(
                trip_id=new_trip_id,
                rider_id=trip_request.rider_id,
                pickup_zone=trip_request.pickup_zone,
                drop_zone=trip_request.drop_zone,
                status=TripStatus.REQUESTED,
                requested_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                eta_minutes=mock_eta_minutes,
                distance_km=mock_distance,
                base_fare=mock_base_fare,
                surge_multiplier=mock_surge,
                total_fare=mock_total_fare
            )
            
            trip_dict = new_trip.dict()
            await trips_coll.insert_one(trip_dict)
            
            return new_trip
        
        except DuplicateKeyError:
            pass

    raise HTTPException(status_code=500, detail="Failed to create trip after multiple attempts.")

@app.get("/v1/trips/{trip_id}", response_model=Trip)
async def get_trip_details(
    trip_id: int,
    trips_coll: AsyncIOMotorCollection = Depends(get_trips_collection)
):
    trip_doc = await trips_coll.find_one({"trip_id": trip_id})
    if not trip_doc:
        raise HTTPException(status_code=404, detail=f"Trip with id {trip_id} not found")
    return Trip(**trip_doc)

@app.post("/v1/trips/{trip_id}/accept", response_model=Trip)
async def accept_trip(
    trip_id: int,
    request: AcceptTripRequest,
    trips_coll: AsyncIOMotorCollection = Depends(get_trips_collection)
):
    trip_doc = await trips_coll.find_one({"trip_id": trip_id})
    if not trip_doc:
        raise HTTPException(status_code=404, detail=f"Trip with id {trip_id} not found")
    
    trip = Trip(**trip_doc)

    if trip.status != TripStatus.REQUESTED:
        raise HTTPException(status_code=400, detail="Trip is not in 'REQUESTED' state")
        
    driver_url = f"{DRIVER_SERVICE_URL}/drivers/{request.driver_id}"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(driver_url)
            response.raise_for_status()
            driver_json = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Driver not found")
        raise HTTPException(status_code=503, detail="Driver service is unavailable")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Error connecting to Driver service")

    if isinstance(driver_json, dict):
        if "id" not in driver_json and "driver_id" in driver_json:
            driver_json["id"] = driver_json["driver_id"]

        if "is_active" in driver_json:
            val = driver_json["is_active"]
            if isinstance(val, str):
                driver_json["is_active"] = val.strip().lower() in ("true", "1", "yes", "y")
            else:
                driver_json["is_active"] = bool(val)

    try:
        driver_data = MockDriver(**driver_json)
    except ValidationError as e:
        raise HTTPException(status_code=502, detail=f"Driver service returned invalid data: {e}")

    if not driver_data.is_active:
        raise HTTPException(status_code=400, detail="Driver is not active")
        
    update_data = {
        "$set": {
            "driver_id": request.driver_id,
            "status": TripStatus.ACCEPTED,
            "updated_at": datetime.utcnow()
        }
    }
    
    updated_doc = await trips_coll.find_one_and_update(
        {"trip_id": trip_id},
        update_data,
        return_document=True
    )
    
    return Trip(**updated_doc)

@app.post("/v1/trips/{trip_id}/complete", response_model=Trip)
async def complete_trip(
    trip_id: int,
    trips_coll: AsyncIOMotorCollection = Depends(get_trips_collection)
):
    trip_doc = await trips_coll.find_one({"trip_id": trip_id})
    if not trip_doc:
        raise HTTPException(status_code=404, detail=f"Trip with id {trip_id} not found")
        
    trip = Trip(**trip_doc)

    if trip.status not in [TripStatus.ACCEPTED, TripStatus.ONGOING]:
         raise HTTPException(status_code=400, detail="Trip is not in an active state")

    payment_payload = {
        "trip_id": trip.trip_id,
        "amount": trip.total_fare,
        "rider_id": trip.rider_id,
        "method": "card"
    }
    payment_url = f"{PAYMENT_SERVICE_URL}/payments/charge"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(payment_url, json=payment_payload)
            response.raise_for_status()
            
            final_status = TripStatus.COMPLETED

    except httpx.HTTPStatusError:
        raise HTTPException(
            status_code=402,
            detail="Payment processing failed. Trip not completed."
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Payment service is unavailable")

    update_data = {
        "$set": {
            "status": final_status,
            "updated_at": datetime.utcnow()
        }
    }
    
    updated_doc = await trips_coll.find_one_and_update(
        {"trip_id": trip_id},
        update_data,
        return_document=True
    )

    return Trip(**updated_doc)

@app.post("/v1/trips/{trip_id}/cancel", response_model=Trip)
async def cancel_trip(
    trip_id: int,
    trips_coll: AsyncIOMotorCollection = Depends(get_trips_collection)
):
    trip_doc = await trips_coll.find_one({"trip_id": trip_id})
    if not trip_doc:
        raise HTTPException(status_code=404, detail=f"Trip with id {trip_id} not found")
        
    trip = Trip(**trip_doc)

    if trip.status in [TripStatus.COMPLETED, TripStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Cannot cancel a finished trip")
        
    update_data = {
        "$set": {
            "status": TripStatus.CANCELLED,
            "updated_at": datetime.utcnow()
        }
    }

    if trip.status in [TripStatus.ACCEPTED, TripStatus.ONGOING]:
        payment_payload = {
            "trip_id": f"cancel_{trip.trip_id}",
            "amount": CANCELLATION_FEE,
            "rider_id": trip.rider_id,
            "method": "cancellation_fee"
        }
        payment_url = f"{PAYMENT_SERVICE_URL}/payments/charge"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(payment_url, json=payment_payload)
                response.raise_for_status()
                update_data["$set"]["total_fare"] = trip.total_fare + CANCELLATION_FEE
        except Exception:
            raise HTTPException(status_code=402, detail="Cancellation fee processing failed")
    
    updated_doc = await trips_coll.find_one_and_update(
        {"trip_id": trip_id},
        update_data,
        return_document=True
    )
    
    return Trip(**updated_doc)