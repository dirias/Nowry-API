# app/models/Page.py
from pydantic import BaseModel


class Page(BaseModel):
    title: str
    page_number: int
    content: str
