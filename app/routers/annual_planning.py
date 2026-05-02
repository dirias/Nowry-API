from fastapi import APIRouter, Depends, HTTPException, Body
from typing import List, Optional
from datetime import datetime, timezone
from bson import ObjectId

from app.auth.firebase_auth import get_firebase_user
from app.models.common import MessageResponse, OkResponse, FullAnnualPlanResponse
from app.config.database import (
    annual_plans_collection,
    focus_areas_collection,
    priorities_collection,
    goals_collection,
    activities_collection,
    daily_routines_collection,
    quarter_reports_collection,
    books_collection,
)
from app.models.AnnualPlan import AnnualPlan
from app.models.FocusArea import FocusArea
from app.models.Priority import Priority
from app.models.Goal import Goal
from app.models.Activity import Activity
from app.models.DailyRoutine import DailyRoutineTemplate
from app.models.QuarterReport import QuarterReport

async def verify_annual_plan_ownership(plan_id: str, user_id: str):
    from bson.errors import InvalidId
    try:
        obj_id = ObjectId(plan_id)
    except InvalidId:
        obj_id = plan_id
    plan = await annual_plans_collection.find_one({"_id": obj_id})
    if not plan or str(plan.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Not authorized to access this plan data")

async def verify_focus_area_ownership(fa_id: str, user_id: str):
    from bson.errors import InvalidId
    try:
        obj_id = ObjectId(fa_id)
    except InvalidId:
        obj_id = fa_id
    fa = await focus_areas_collection.find_one({"_id": obj_id})
    if not fa: raise HTTPException(status_code=404, detail="Focus area not found")
    await verify_annual_plan_ownership(fa["annual_plan_id"], user_id)

async def verify_goal_ownership(goal_id: str, user_id: str):
    from bson.errors import InvalidId
    try:
        obj_id = ObjectId(goal_id)
    except InvalidId:
        obj_id = goal_id
    goal = await goals_collection.find_one({"_id": obj_id})
    if not goal: raise HTTPException(status_code=404, detail="Goal not found")
    await verify_focus_area_ownership(goal["focus_area_id"], user_id)

async def verify_priority_ownership(priority_id: str, user_id: str):
    from bson.errors import InvalidId
    try:
        obj_id = ObjectId(priority_id)
    except InvalidId:
        obj_id = priority_id
    p = await priorities_collection.find_one({"_id": obj_id})
    if not p: raise HTTPException(status_code=404, detail="Priority not found")
    await verify_annual_plan_ownership(p["annual_plan_id"], user_id)


router = APIRouter(
    prefix="/annual-plan",
    tags=["annual-planning"],
    responses={404: {"description": "Not found"}},
)


# --- Daily Routine ---

@router.get("/daily-routine", response_model=DailyRoutineTemplate)
async def get_daily_routine(
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    routine = await daily_routines_collection.find_one({"user_id": user_id})
    
    if not routine:
        new_routine = DailyRoutineTemplate(user_id=user_id)
        result = await daily_routines_collection.insert_one(new_routine.model_dump(by_alias=True))
        routine = await daily_routines_collection.find_one({"_id": result.inserted_id})
        
    return routine

@router.put("/daily-routine", response_model=DailyRoutineTemplate)
async def update_daily_routine(
    routine: DailyRoutineTemplate,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    
    # Update by user_id since it's a singleton per user
    # This avoids issues with stale IDs from the frontend
    
    result = await daily_routines_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "morning_routine": routine.morning_routine,
            "afternoon_routine": routine.afternoon_routine,
            "evening_routine": routine.evening_routine,
            "updated_at": datetime.now()
        }}
    )
    
    if result.matched_count == 0:
        # If for some reason it doesn't exist (shouldn't happen if GET was called, but safety first)
        # We can create it or raise 404. Let's raise 404 to be safe, or just insert.
        # Given the previous flow, let's create it implicitly if we want to be super robust, 
        # but technically the user should have GET it first.
        # Let's revert to finding it to return it.
        # Actually, let's try to upsert.
        
        # Check if we should insert? 
        # For now, let's raise 404 but with a clear message, or create a new one.
        # Let's create a new one to be helpful.
        new_routine = DailyRoutineTemplate(
            user_id=user_id,
            morning_routine=routine.morning_routine,
            afternoon_routine=routine.afternoon_routine,
            evening_routine=routine.evening_routine
        )
        insert_result = await daily_routines_collection.insert_one(new_routine.model_dump(by_alias=True))
        return await daily_routines_collection.find_one({"_id": insert_result.inserted_id})

    return await daily_routines_collection.find_one({"user_id": user_id})


@router.patch("/daily-routine/completions", response_model=OkResponse)
async def update_routine_completions(
    payload: dict = Body(...),
    current_user: dict = Depends(get_firebase_user),
):
    """
    Update only the completion list for a specific date.
    Uses dot-notation $set so other dates are untouched.
    Expected payload: { "date": "YYYY-MM-DD", "items": ["morning_0", "evening_2"] }
    """
    user_id = current_user.get("user_id")
    date_key = payload.get("date")
    items = payload.get("items", [])

    if not date_key:
        raise HTTPException(status_code=400, detail="date field is required")

    await daily_routines_collection.update_one(
        {"user_id": user_id},
        {"$set": {f"daily_completions.{date_key}": items}},
        upsert=True,
    )
    return {"ok": True}


# --- Annual Plan ---

@router.get("", response_model=AnnualPlan)
async def get_annual_plan(
    current_user: dict = Depends(get_firebase_user),
    year: int = datetime.now().year
):
    user_id = current_user.get("user_id")
    plan = await annual_plans_collection.find_one({"user_id": user_id, "year": year, "deleted_at": None})

    if not plan:
        raise HTTPException(status_code=404, detail="No annual plan found for this year")

    return plan


@router.get("/full", response_model=FullAnnualPlanResponse)
async def get_full_annual_plan(
    current_user: dict = Depends(get_firebase_user),
    year: int = datetime.now().year
):
    """
    Aggregation endpoint — returns the plan, focus areas, priorities, and all
    goals for every focus area in a single round-trip.

    Replaces the 3-level sequential waterfall:
      /annual-plan  →  /focus-areas + /priorities  →  /goals × N

    All DB queries run concurrently via asyncio.gather so the response time is
    bounded by the slowest single query, not the sum of all of them.
    """
    import asyncio

    user_id = current_user.get("user_id")
    plan = await annual_plans_collection.find_one({"user_id": user_id, "year": year, "deleted_at": None})

    if not plan:
        raise HTTPException(status_code=404, detail="No annual plan found for this year")

    plan_id = str(plan["_id"])

    # Level 2: focus areas, priorities, and quarter reports in parallel
    areas_coro = focus_areas_collection.find({"annual_plan_id": plan_id, "deleted_at": None}).to_list(length=10)
    priorities_coro = priorities_collection.find({"annual_plan_id": plan_id, "deleted_at": None}).to_list(length=50)
    reports_coro = quarter_reports_collection.find({"annual_plan_id": plan_id, "deleted_at": None}).to_list(length=10)

    areas, priorities, reports = await asyncio.gather(areas_coro, priorities_coro, reports_coro)

    # Level 3: goals for every area — all in parallel
    goal_lists = await asyncio.gather(
        *[goals_collection.find({"focus_area_id": str(area["_id"]), "deleted_at": None}).to_list(length=100) for area in areas]
    )

    # Attach goals to each area and build flat goals list
    goals_by_area = {}
    all_goals = []
    goal_ids = []
    for area, area_goals in zip(areas, goal_lists):
        area_id = str(area["_id"])
        goals_by_area[area_id] = area_goals
        all_goals.extend(area_goals)
        goal_ids.extend([str(g["_id"]) for g in area_goals])

    # Level 4: activities for all goals
    all_activities = []
    if goal_ids:
        all_activities = await activities_collection.find({"goal_id": {"$in": goal_ids}, "deleted_at": None}).to_list(length=500)

    def serialize(doc):
        """Convert ObjectId and other non-serializable types to strings."""
        if doc is None:
            return None
        result = {}
        for k, v in doc.items():
            if hasattr(v, '__str__') and type(v).__name__ == 'ObjectId':
                result[k] = str(v)
            elif isinstance(v, list):
                result[k] = [serialize(i) if isinstance(i, dict) else i for i in v]
            elif isinstance(v, dict):
                result[k] = serialize(v)
            else:
                result[k] = v
        return result

    # Calculate Overdue Quarter Logic purely on the backend
    now = datetime.now()
    current_q_year = now.year
    current_q = (now.month - 1) // 3 + 1
    
    overdue_q = None
    overdue_year = None
    
    plan_year = plan.get("year", current_q_year)
    past_quarters = [q for q in [1, 2, 3, 4] if current_q_year > plan_year or (current_q_year == plan_year and current_q > q)]
    
    for pq in past_quarters:
        has_report = any(r.get("quarter") == pq and r.get("year") == plan_year for r in reports)
        has_goals = any(g.get("quarter") == pq and g.get("year") == plan_year for g in all_goals)
        if not has_report and has_goals:
            overdue_q = pq
            overdue_year = plan_year
            break
            
    plan_serialized = serialize(plan)
    if overdue_q:
        plan_serialized["overdue_quarter"] = {"quarter": overdue_q, "year": overdue_year}

    return {
        "plan": plan_serialized,
        "focus_areas": [serialize(a) for a in areas],
        "priorities": [serialize(p) for p in priorities],
        "goals": [serialize(g) for g in all_goals],
        "activities": [serialize(act) for act in all_activities],
        "quarter_reports": [serialize(r) for r in reports],
    }

@router.post("", response_model=AnnualPlan, status_code=201)
async def create_annual_plan(
    plan_data: dict = Body(...),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    year = plan_data.get("year", datetime.now().year)
    
    # Check if plan already exists
    existing_plan = await annual_plans_collection.find_one({"user_id": user_id, "year": year})
    if existing_plan:
        raise HTTPException(status_code=400, detail=f"Annual plan for {year} already exists")
    
    # Create new plan
    new_plan = AnnualPlan(
        user_id=user_id,
        year=year,
        title=plan_data.get("title", f"My {year} Plan")
    )
    result = await annual_plans_collection.insert_one(new_plan.model_dump(by_alias=True))
    plan = await annual_plans_collection.find_one({"_id": result.inserted_id})
    
    return plan

@router.put("", response_model=AnnualPlan)
async def update_annual_plan(
    plan_update: AnnualPlan,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    # We only allow updating the current year's plan for now or based on ID if we passed it, 
    # but here we follow the GET pattern (current year implied or passed in body)
    # Ideally we update by ID, but let's assume one active plan for the context.
    # Actually, let's use the ID from the body or find by year.
    # For safety, we should probably update by ID.
    
    existing_plan = await annual_plans_collection.find_one({"_id": ObjectId(plan_update.id)})
    if not existing_plan:
        raise HTTPException(status_code=404, detail="Plan not found")
        
    if existing_plan["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    update_data = {
        "title": plan_update.title,
        "updated_at": datetime.now()
    }
    
    await annual_plans_collection.update_one(
        {"_id": ObjectId(plan_update.id)},
        {"$set": update_data}
    )
    
    return await annual_plans_collection.find_one({"_id": ObjectId(plan_update.id)})

@router.put("/{id}", response_model=AnnualPlan)
async def update_annual_plan_by_id(
    id: str,
    plan_update: dict,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    
    # Try to find plan by string ID first (seems to be stored as string in DB)
    print(f"Looking for plan with ID: {id}, user: {user_id}")
    existing_plan = await annual_plans_collection.find_one({"_id": id})
    
    # If not found, try as ObjectId
    if not existing_plan:
        try:
            object_id = ObjectId(id)
            existing_plan = await annual_plans_collection.find_one({"_id": object_id})
        except:
            pass
    
    print(f"Found plan: {existing_plan}")
    
    if not existing_plan:
        raise HTTPException(status_code=404, detail="Plan not found")
        
    if existing_plan["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Only update allowed fields
    update_data = {}
    if "title" in plan_update:
        update_data["title"] = plan_update["title"]
    update_data["updated_at"] = datetime.now()
    
    await annual_plans_collection.update_one(
        {"_id": id},  # Use string ID
        {"$set": update_data}
    )
    
    return await annual_plans_collection.find_one({"_id": id})


@router.delete("/{id}", response_model=MessageResponse)
async def delete_annual_plan(
    id: str,
    current_user: dict = Depends(get_firebase_user),
):
    """
    Soft delete an annual plan and cascade to all related focus areas, goals,
    activities, and priorities. All data will be recoverable within 30 days.
    """
    user_id = current_user.get("user_id")
    await verify_annual_plan_ownership(id, user_id)

    try:
        obj_id = ObjectId(id)
    except Exception:
        obj_id = id

    now = datetime.now(timezone.utc)
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now,
        }
    }

    # 1. Soft-delete the plan itself
    await annual_plans_collection.update_one({"_id": obj_id}, soft_delete_update)

    # 2. CASCADE: Soft-delete all focus areas belonging to this plan
    await focus_areas_collection.update_many(
        {"annual_plan_id": id, "deleted_at": None},
        soft_delete_update,
    )

    # 3. Get focus_area IDs to cascade further (include already-deleted ones for completeness)
    focus_area_ids = [
        str(fa["_id"])
        for fa in await focus_areas_collection.find(
            {"annual_plan_id": id}
        ).to_list(length=1000)
    ]

    if focus_area_ids:
        # 4. CASCADE: Soft-delete all goals linked to these focus areas
        await goals_collection.update_many(
            {"focus_area_id": {"$in": focus_area_ids}, "deleted_at": None},
            soft_delete_update,
        )
        # 5. CASCADE: Soft-delete all activities linked to these focus areas
        await activities_collection.update_many(
            {"focus_area_id": {"$in": focus_area_ids}, "deleted_at": None},
            soft_delete_update,
        )
        # 6. CASCADE: Soft-delete all priorities linked to these focus areas
        await priorities_collection.update_many(
            {"focus_area_id": {"$in": focus_area_ids}, "deleted_at": None},
            soft_delete_update,
        )

    return {"message": "Annual plan and all related data deleted successfully"}


# --- Quarter Reports ---

@router.post("/close-quarter", response_model=QuarterReport)
async def close_quarter(
    payload: dict = Body(...),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    year = payload.get("year")
    quarter = payload.get("quarter")
    annual_plan_id = payload.get("annual_plan_id")
    migrated_goals = payload.get("migrated_goals", [])
    reflections = payload.get("reflections", {})

    if not all([year, quarter, annual_plan_id]):
        raise HTTPException(status_code=400, detail="Missing required fields")

    # Determine quarter dates
    if quarter == 1:
        start_date = datetime(year, 1, 1)
        end_date = datetime(year, 3, 31, 23, 59, 59)
    elif quarter == 2:
        start_date = datetime(year, 4, 1)
        end_date = datetime(year, 6, 30, 23, 59, 59)
    elif quarter == 3:
        start_date = datetime(year, 7, 1)
        end_date = datetime(year, 9, 30, 23, 59, 59)
    else:
        start_date = datetime(year, 10, 1)
        end_date = datetime(year, 12, 31, 23, 59, 59)

    # 1. Fetch all goals currently in this quarter BEFORE migrating them
    focus_areas = await focus_areas_collection.find({"annual_plan_id": annual_plan_id}).to_list(length=100)
    fa_ids = [str(fa["_id"]) for fa in focus_areas]
    
    current_goals = await goals_collection.find({
        "focus_area_id": {"$in": fa_ids},
        "quarter": quarter,
        "year": year,
        "deleted_at": None
    }).to_list(length=500)

    # 2. Calculate metrics
    total_goals = len(current_goals)
    completed_goals = 0
    total_milestones = 0
    completed_milestones = 0
    
    for g in current_goals:
        if g.get("status") == "completed" or g.get("progress", 0) == 100:
            completed_goals += 1
            
        milestones = g.get("milestones", [])
        total_milestones += len(milestones)
        completed_milestones += sum(1 for m in milestones if m.get("completed"))

    if total_goals == 0:
        progress_percentage = 0
    else:
        progress_percentage = int((sum(g.get("progress", 0) for g in current_goals) / total_goals))

    def serialize_goal(g):
        g_dict = dict(g)
        g_dict["_id"] = str(g_dict["_id"])
        if "target_date" in g_dict and isinstance(g_dict["target_date"], datetime):
            g_dict["target_date"] = g_dict["target_date"].isoformat()
        if "created_at" in g_dict and isinstance(g_dict["created_at"], datetime):
            g_dict["created_at"] = g_dict["created_at"].isoformat()
        if "updated_at" in g_dict and isinstance(g_dict["updated_at"], datetime):
            g_dict["updated_at"] = g_dict["updated_at"].isoformat()
        if "completed_at" in g_dict and isinstance(g_dict["completed_at"], datetime):
            g_dict["completed_at"] = g_dict["completed_at"].isoformat()
        return g_dict

    goals_summary = [serialize_goal(g) for g in current_goals]

    # 3. Perform the migration
    for m_goal in migrated_goals:
        goal_id = m_goal.get("id")
        new_quarter = m_goal.get("new_quarter")
        new_target_date = m_goal.get("new_target_date")
        if goal_id and new_quarter and new_target_date:
            try:
                obj_id = ObjectId(goal_id)
            except:
                obj_id = goal_id
            
            # parse date string if it's string format (from frontend)
            dt = new_target_date
            if isinstance(dt, str):
                dt = dt.replace('Z', '+00:00')
                dt = datetime.fromisoformat(dt)
            
            update_payload = {
                "$set": {
                    "quarter": new_quarter,
                    "target_date": dt,
                    "updated_at": datetime.now()
                },
                "$inc": {"migration_count": 1}
            }
            
            res = await goals_collection.update_one({"_id": obj_id}, update_payload)
            if res.matched_count == 0:
                await goals_collection.update_one({"_id": str(goal_id)}, update_payload)

    # New: Aggregate Knowledge (Books finished in this quarter)
    books = await books_collection.find({
        "user_id": user_id,
        "status": "completed",
        "updated_at": {"$gte": start_date, "$lte": end_date}
    }).to_list(length=100)
    
    def serialize_book(b):
        b["_id"] = str(b["_id"])
        if "created_at" in b and isinstance(b["created_at"], datetime):
            b["created_at"] = b["created_at"].isoformat()
        if "updated_at" in b and isinstance(b["updated_at"], datetime):
            b["updated_at"] = b["updated_at"].isoformat()
        return b
        
    knowledge_summary = {
        "books_finished": [serialize_book(b) for b in books],
        "cards_mastered_count": 0 # Placeholder for study center integration if needed later
    }

    # New: Aggregate Routines (Days active in this quarter)
    routine_doc = await daily_routines_collection.find_one({"user_id": user_id})
    routines_summary = {"average_completion_rate": 0.0, "active_days": 0}
    if routine_doc and "daily_completions" in routine_doc:
        completions = routine_doc["daily_completions"]
        active_days = 0
        total_items_checked = 0
        for date_str, items in completions.items():
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                if start_date <= d <= end_date:
                    if len(items) > 0:
                        active_days += 1
                        total_items_checked += len(items)
            except:
                continue
                
        # Simple heuristic: active vs total days in quarter (~90 days)
        routines_summary = {
            "active_days": active_days,
            "total_items_checked": total_items_checked,
            "completion_rate": min(100, int((active_days / 90) * 100)) if active_days > 0 else 0
        }

    # 4. Create and save QuarterReport
    report = QuarterReport(
        user_id=user_id,
        annual_plan_id=annual_plan_id,
        year=year,
        quarter=quarter,
        total_goals=total_goals,
        completed_goals=completed_goals,
        total_milestones=total_milestones,
        completed_milestones=completed_milestones,
        progress_percentage=progress_percentage,
        goals_summary=goals_summary,
        reflections=reflections,
        routines_summary=routines_summary,
        knowledge_summary=knowledge_summary
    )
    
    result = await quarter_reports_collection.insert_one(report.model_dump(by_alias=True))
    created_report = await quarter_reports_collection.find_one({"_id": result.inserted_id})
    return created_report

@router.get("/quarter-reports/{annual_plan_id}", response_model=List[QuarterReport])
async def get_quarter_reports(
    annual_plan_id: str,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    reports = await quarter_reports_collection.find({
        "annual_plan_id": annual_plan_id,
        "user_id": user_id,
        "deleted_at": None
    }).sort("quarter", 1).to_list(length=10)
    return reports


# --- Focus Areas ---

@router.get("/focus-areas", response_model=List[FocusArea])
async def get_focus_areas(
    annual_plan_id: str,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_annual_plan_ownership(annual_plan_id, user_id)
    areas = await focus_areas_collection.find({"annual_plan_id": annual_plan_id, "deleted_at": None}).to_list(length=10)
    return areas

@router.post("/focus-areas", response_model=FocusArea)
async def create_focus_area(
    focus_area: FocusArea,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_annual_plan_ownership(focus_area.annual_plan_id, user_id)
    
    # Check limit (max 3)
    count = await focus_areas_collection.count_documents({"annual_plan_id": focus_area.annual_plan_id})
    if count >= 3:
        raise HTTPException(status_code=400, detail="Maximum 3 focus areas allowed")
        
    result = await focus_areas_collection.insert_one(focus_area.model_dump(by_alias=True))
    created = await focus_areas_collection.find_one({"_id": result.inserted_id})
    return created

@router.put("/focus-areas/{id}", response_model=FocusArea)
async def update_focus_area(
    id: str,
    focus_area: FocusArea,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_focus_area_ownership(id, user_id)
    
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    result = await focus_areas_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "name": focus_area.name, 
            "description": focus_area.description,
            "color": focus_area.color,
            "icon": focus_area.icon,
            "updated_at": datetime.now()
        }}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Focus Area not found")
        
    return await focus_areas_collection.find_one({"_id": obj_id})

@router.delete("/focus-areas/{id}", response_model=MessageResponse)
async def delete_focus_area(
    id: str,
    current_user: dict = Depends(get_firebase_user),
):
    """
    Soft delete a focus area and cascade to related goals, activities, and priorities.
    All related data will be soft-deleted and can be recovered within 30 days.
    """
    from datetime import datetime, timezone

    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    # 1. Verify ownership
    focus_area = await focus_areas_collection.find_one({
        "_id": ObjectId(id),
        "user_id": user_id,
        "deleted_at": None
    })
    
    if not focus_area:
        raise HTTPException(status_code=404, detail="Focus area not found")
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now
        }
    }
    
    # 2. Soft delete the focus area
    await focus_areas_collection.update_one(
        {"_id": ObjectId(id)},
        soft_delete_update
    )
    
    # 3. CASCADE: Soft delete all related goals
    await goals_collection.update_many(
        {"focus_area_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    # 4. CASCADE: Soft delete all activities in these goals
    await activities_collection.update_many(
        {"focus_area_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    # 5. CASCADE: Soft delete all related priorities
    await priorities_collection.update_many(
        {"focus_area_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    return {"message": "Focus area and all related data deleted successfully"}


# --- Priorities ---

@router.get("/priorities", response_model=List[Priority])
async def get_priorities(
    annual_plan_id: str,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_annual_plan_ownership(annual_plan_id, user_id)
    priorities = await priorities_collection.find({"annual_plan_id": annual_plan_id, "deleted_at": None}).to_list(length=50)
    return priorities

@router.post("/priorities", response_model=Priority)
async def create_priority(
    priority: Priority,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_annual_plan_ownership(priority.annual_plan_id, user_id)
    result = await priorities_collection.insert_one(priority.model_dump(by_alias=True))
    return await priorities_collection.find_one({"_id": result.inserted_id})

@router.put("/priorities/{id}", response_model=Priority)
async def update_priority(
    id: str,
    priority_update: dict,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_priority_ownership(id, user_id)
    try:
        obj_id = ObjectId(id)
    except Exception as e:
        print(f"[DEBUG] Invalid ObjectId format: {id}, error: {e}")
        raise HTTPException(status_code=400, detail="Invalid ID format")

    # Debug: Check if priority exists
    existing = await priorities_collection.find_one({"_id": obj_id})
    if not existing:
        print(f"[DEBUG] Priority not found with ObjectId: {obj_id}")
        # Try finding with string ID
        existing = await priorities_collection.find_one({"_id": id})
        if not existing:
            print(f"[DEBUG] Priority not found with string ID either: {id}")
            raise HTTPException(status_code=404, detail=f"Priority not found: {id}")
        # Use string ID for update
        obj_id = id

    print(f"[DEBUG] Found priority: {existing.get('title')} with ID: {obj_id}")

    # Build update dict with only provided fields
    update_data = {"updated_at": datetime.now()}
    
    if "title" in priority_update:
        update_data["title"] = priority_update["title"]
    if "description" in priority_update:
        update_data["description"] = priority_update["description"]
    if "deadline" in priority_update:
        update_data["deadline"] = priority_update["deadline"]
    if "is_completed" in priority_update:
        update_data["is_completed"] = priority_update["is_completed"]
        update_data["completed_at"] = datetime.now() if priority_update["is_completed"] else None
    if "linked_entity_id" in priority_update:
        update_data["linked_entity_id"] = priority_update["linked_entity_id"]
    if "linked_entity_type" in priority_update:
        update_data["linked_entity_type"] = priority_update["linked_entity_type"]
    if "annual_plan_id" in priority_update:
        update_data["annual_plan_id"] = priority_update["annual_plan_id"]
    if "focus_area_id" in priority_update:
        update_data["focus_area_id"] = priority_update["focus_area_id"]

    print(f"[DEBUG] Updating priority with data: {update_data}")

    result = await priorities_collection.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        print(f"[DEBUG] Update matched 0 documents for ID: {obj_id}")
        raise HTTPException(status_code=404, detail="Priority not found")

    print(f"[DEBUG] Successfully updated priority")
    return await priorities_collection.find_one({"_id": obj_id})

@router.delete("/priorities/{id}", response_model=MessageResponse)
async def delete_priority(id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")
    await verify_priority_ownership(id, user_id)

    try:
        obj_id = ObjectId(id)
    except Exception:
        obj_id = id

    now = datetime.now(timezone.utc)
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now,
        }
    }
    result = await priorities_collection.update_one({"_id": obj_id}, soft_delete_update)
    if result.matched_count == 0:
        # Try string ID as fallback
        result = await priorities_collection.update_one({"_id": id}, soft_delete_update)

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Priority not found")

    return {"message": "Priority deleted"}


# --- Goals ---

@router.get("/goals", response_model=List[Goal])
async def get_goals(
    focus_area_id: Optional[str] = None,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    query = {}
    if focus_area_id:
        await verify_focus_area_ownership(focus_area_id, user_id)
        query["focus_area_id"] = focus_area_id
    
    # We filter by focus_area_id which is strictly ownership validated
    query["deleted_at"] = None
    goals = await goals_collection.find(query).to_list(length=100)
    return goals

@router.post("/goals", response_model=Goal)
async def create_goal(
    goal: Goal,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_focus_area_ownership(goal.focus_area_id, user_id)
    result = await goals_collection.insert_one(goal.model_dump(by_alias=True))
    return await goals_collection.find_one({"_id": result.inserted_id})

@router.put("/goals/{id}", response_model=Goal)
async def update_goal(
    id: str,
    goal_update: dict,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    await verify_goal_ownership(id, user_id)
    # Try validating/converting to ObjectId
    try:
        obj_id = ObjectId(id)
        
        # Build update dict with only provided fields
        update_data = {"updated_at": datetime.now()}
        
        if "title" in goal_update:
            update_data["title"] = goal_update["title"]
        if "description" in goal_update:
            update_data["description"] = goal_update["description"]
        if "image_url" in goal_update:
            update_data["image_url"] = goal_update["image_url"]
        if "target_date" in goal_update:
            update_data["target_date"] = goal_update["target_date"]
        if "progress" in goal_update:
            update_data["progress"] = goal_update["progress"]
        if "status" in goal_update:
            update_data["status"] = goal_update["status"]
        if "milestones" in goal_update:
            update_data["milestones"] = goal_update["milestones"]
        if "parent_id" in goal_update:
            update_data["parent_id"] = goal_update["parent_id"]
        if "quarter" in goal_update:
            update_data["quarter"] = goal_update["quarter"]
        if "year" in goal_update:
            update_data["year"] = goal_update["year"]
        if "type" in goal_update:
            update_data["type"] = goal_update["type"]
        
        # Try updating with ObjectId
        result = await goals_collection.update_one(
            {"_id": obj_id},
            {"$set": update_data}
        )
        # If no match, maybe it's stored as a string?
        if result.matched_count == 0:
             result = await goals_collection.update_one(
                {"_id": id},
                {"$set": update_data}
            )
             if result.matched_count > 0:
                 # It was a string ID
                 obj_id = id
             else:
                 raise HTTPException(status_code=404, detail="Goal not found")

    except Exception:
        # If ObjectId conversion failed completely, try as string directly
        result = await goals_collection.update_one(
            {"_id": id},
            {"$set": update_data}
        )
        if result.matched_count == 0:
             raise HTTPException(status_code=404, detail="Goal not found")
        obj_id = id

    # --- Priority cascade on status change ---
    # When a goal is completed, archive all priorities linked to it.
    # When a goal is un-completed, reactivate those priorities.
    if "status" in goal_update:
        new_status = goal_update["status"]
        goal_id_str = str(id)
        now = datetime.now()

        if new_status == "completed":
            priority_update = {
                "is_completed": True,
                "completed_at": now,
                "updated_at": now,
            }
        else:
            # Goal moved back to in_progress or not_started → reactivate
            priority_update = {
                "is_completed": False,
                "completed_at": None,
                "updated_at": now,
            }

        await priorities_collection.update_many(
            {"linked_entity_id": goal_id_str, "linked_entity_type": "goal"},
            {"$set": priority_update},
        )

    updated_goal = await goals_collection.find_one({"_id": obj_id})
    return updated_goal

@router.delete("/goals/{id}", response_model=MessageResponse)
async def delete_goal(id: str, current_user: dict = Depends(get_firebase_user)):
    """
    Soft delete a goal and cascade to related activities.
    All related data will be soft-deleted and can be recovered within 30 days.
    """
    from datetime import datetime, timezone

    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    # 1. Verify ownership
    goal = await goals_collection.find_one({
        "_id": ObjectId(id),
        "user_id": user_id,
        "deleted_at": None
    })
    
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now
        }
    }
    
    # 2. Soft delete the goal
    await goals_collection.update_one(
        {"_id": ObjectId(id)},
        soft_delete_update
    )
    
    # 3. CASCADE: Soft delete all related activities
    await activities_collection.update_many(
        {"goal_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    return {"message": "Goal and all activities deleted successfully"}


# --- Activities ---

@router.get("/goals/{goal_id}/activities", response_model=List[Activity])
async def get_activities(
    goal_id: str,
    current_user: dict = Depends(get_firebase_user),
):
    activities = await activities_collection.find({"goal_id": goal_id, "deleted_at": None}).to_list(length=50)
    return activities

@router.post("/goals/{goal_id}/activities", response_model=Activity)
async def create_activity(
    goal_id: str,
    activity: Activity,
    current_user: dict = Depends(get_firebase_user),
):
    activity.goal_id = goal_id
    result = await activities_collection.insert_one(activity.model_dump(by_alias=True))
    return await activities_collection.find_one({"_id": result.inserted_id})

@router.put("/activities/{id}", response_model=Activity)
async def update_activity(
    id: str,
    activity: Activity,
    current_user: dict = Depends(get_firebase_user),
):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    result = await activities_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "title": activity.title,
            "frequency": activity.frequency,
            "days_of_week": activity.days_of_week,
            "time_of_day": activity.time_of_day,
            "is_active": activity.is_active,
            "updated_at": datetime.now()
        }}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Activity not found")

    return await activities_collection.find_one({"_id": obj_id})

@router.delete("/activities/{id}", response_model=MessageResponse)
async def delete_activity(id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now,
        }
    }
    result = await activities_collection.update_one({"_id": ObjectId(id)}, soft_delete_update)
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Activity not found")
    return {"message": "Activity deleted"}



