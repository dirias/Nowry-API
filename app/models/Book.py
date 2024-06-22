# models.py
from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId  # Import PyObjectId from the types module


class Book(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    title: str
    author: str
    isbn: str

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}
