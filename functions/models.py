# In app/models.py
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional
from datetime import datetime
from bson import ObjectId

class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid objectid")
        return ObjectId(v)

    @classmethod
    def __modify_schema__(cls, field_schema):
        field_schema.update(type="string")


class TripStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    ONGOING = "ONGOING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

class TripCreate(BaseModel):
    rider_id: int
    pickup_zone: str
    drop_zone: str

class Trip(BaseModel):
    trip_id: int
    rider_id: int
    driver_id: Optional[int] = None
    
    pickup_zone: str
    drop_zone: str
    
    status: TripStatus = TripStatus.REQUESTED
    
    requested_at: str 
    
    distance_km: float
    base_fare: float
    surge_multiplier: float
    total_fare: float
    
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    requested_at: str 
    eta_minutes: Optional[int] = None
    distance_km: float

    class Config:
        allow_population_by_field_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda dt: dt.isoformat()
        }

class AcceptTripRequest(BaseModel):
    driver_id: int

class MockDriver(BaseModel):
    id: int
    is_active: bool