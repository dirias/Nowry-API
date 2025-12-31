from fastapi import APIRouter, Depends, HTTPException
from app.models.Page import Page
from bson import ObjectId
from app.config.database import (
    book_pages_collection,
    books_collection,
    users_collection,
)
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
from pymongo.collection import Collection
from app.auth import get_current_user_authorization

router = APIRouter(
    prefix="/book_page",
    tags=["book pages"],
    responses={404: {"description": "Not found"}},
)


def get_page_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return book_pages_collection


def get_book_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection


@router.post("/save_book_page", response_model=Page)
async def save_book_page(
    book_page: Page,
    book_pages_collection: Collection = Depends(get_page_collection),
    books_collection: Collection = Depends(get_book_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    # Check if the page already exists
    existing_page = await book_pages_collection.find_one(
        {"_id": ObjectId(book_page.id)}
    )
    if existing_page:
        # Update the existing page
        result = await book_pages_collection.update_one(
            {"_id": ObjectId(book_page.id)},
            {"$set": book_page.dict(by_alias=True, exclude={"id"})},
        )
        if not result.acknowledged:
            raise HTTPException(
                status_code=404, detail="Unable to update page {book_page.id}"
            )
    else:
        # --- Subscription Limit Check ---
        user_id = current_user.get("user_id")
        user = await users_collection.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        subscription_data = user.get("subscription", {"tier": "free"})
        tier_key = subscription_data.get("tier", "free")
        plan = SUBSCRIPTION_PLANS.get(
            tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE]
        )

        page_limit = plan["limits"]["pages_per_book"]

        if page_limit != -1:
            current_pages = await book_pages_collection.count_documents(
                {"book_id": book_page.book_id}
            )
            if current_pages >= page_limit:
                raise HTTPException(
                    status_code=403,
                    detail=f"Page limit per book reached ({page_limit}) for {plan['name']} plan. Upgrade to add more pages.",
                )
        # --------------------------------

        # Insert the book page into the MongoDB collection
        result = await book_pages_collection.insert_one(book_page.dict(by_alias=True))

        await books_collection.update_one(
            {"_id": ObjectId(book_page.book_id)},
            {"$push": {"pages": result.inserted_id}},
        )

    return book_page


@router.get("/get_book_page/{book_id}/{page_number}", response_model=Page)
async def get_book_page(book_id: str, page_number: int):
    # Retrieve the book page by book_id and page_number from the MongoDB collection
    book_page = await book_pages_collection.find_one(
        {"_id": book_id, "page_number": page_number}
    )
    if book_page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return book_page


@router.delete("/delete/{page_id}")
async def delete_book_page(page_id: str):
    # Delete the page from book_pages_collection
    delete_result = await book_pages_collection.delete_one({"_id": ObjectId(page_id)})

    if delete_result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Page not found")

    # Remove the page reference from the book
    await books_collection.update_many({}, {"$pull": {"pages": ObjectId(page_id)}})

    return {"message": "Page deleted successfully"}
