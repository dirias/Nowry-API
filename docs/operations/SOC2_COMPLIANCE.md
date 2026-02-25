# SOC 2 Compliance Implementation Guide

## Executive Summary

This document outlines the current state of SOC 2 compliance for the Nowry API soft delete implementation and provides a roadmap for achieving full compliance with SOC 2 Type II requirements.

**Current Status:** ⚠️ Partially Compliant  
**Target:** ✅ Full SOC 2 Type II Compliance  
**Priority:** High (Required for Enterprise customers)

---

## Table of Contents

1. [SOC 2 Trust Services Criteria](#soc-2-trust-services-criteria)
2. [Current Implementation Status](#current-implementation-status)
3. [Compliance Gaps](#compliance-gaps)
4. [Implementation Roadmap](#implementation-roadmap)
5. [Technical Requirements](#technical-requirements)
6. [Data Retention Policy](#data-retention-policy)
7. [Audit Trail Requirements](#audit-trail-requirements)
8. [Security Controls](#security-controls)
9. [Monitoring & Reporting](#monitoring--reporting)
10. [Testing & Validation](#testing--validation)

---

## SOC 2 Trust Services Criteria

SOC 2 focuses on five Trust Services Criteria. For soft delete and data management, we primarily address:

### CC6.1 - Logical and Physical Access Controls
- **Requirement:** Restrict logical access to data based on user roles
- **Application:** Who can delete/restore data must be controlled and audited

### CC7.2 - System Monitoring
- **Requirement:** Monitor system components and alert on anomalies
- **Application:** Monitor delete/restore operations for unusual patterns

### CC7.3 - Evaluate Security Events
- **Requirement:** Evaluate security events to identify security incidents
- **Application:** Track and analyze all data deletion activities

### CC8.1 - Authorize, Modify, Delete
- **Requirement:** Authorize, modify, or delete data to meet business and compliance requirements
- **Application:** Implement proper authorization for deletion/restoration

### PI1.4 - Data Disposal
- **Requirement:** Dispose of personal information to meet regulatory requirements
- **Application:** Permanent deletion after retention period

### PI1.5 - Data Retention
- **Requirement:** Retain personal information for the period required
- **Application:** Enforce data retention policies

---

## Current Implementation Status

### ✅ What We Have

| Feature | Status | Notes |
|---------|--------|-------|
| Soft Delete Mechanism | ✅ Complete | All high-value models support soft delete |
| Deleted By Tracking | ✅ Complete | `deleted_by` field tracks actor |
| Deletion Timestamps | ✅ Complete | `deleted_at` field tracks timing |
| Restore Capability | ✅ Complete | Data can be recovered |
| Mixin Architecture | ✅ Complete | Reusable, consistent implementation |

### ⚠️ Partially Implemented

| Feature | Status | Notes |
|---------|--------|-------|
| Access Controls | ⚠️ Partial | Basic Firebase Auth, needs RBAC |
| Data Classification | ⚠️ Partial | No PII/sensitive data labeling |
| Encryption | ⚠️ Partial | Depends on MongoDB Atlas config |

### ❌ Missing (Critical Gaps)

| Feature | Status | Priority | Impact |
|---------|--------|----------|--------|
| Comprehensive Audit Logging | ❌ Missing | **CRITICAL** | Non-compliant |
| Automated Retention Enforcement | ❌ Missing | **HIGH** | Non-compliant |
| Permanent Deletion Process | ❌ Missing | **HIGH** | Non-compliant |
| Compliance Reporting | ❌ Missing | **MEDIUM** | Non-compliant |
| Data Lineage Tracking | ❌ Missing | **MEDIUM** | Best practice |
| Incident Response Plan | ❌ Missing | **MEDIUM** | Non-compliant |

---

## Compliance Gaps

### 1. Audit Logging (CRITICAL)

**Gap:** No comprehensive audit trail for all data operations.

**SOC 2 Requirement:** CC7.2, CC7.3 - All system events must be logged and monitored.

**Current State:**
- Only tracks `deleted_by` and `deleted_at`
- No logs for CREATE, READ, UPDATE operations
- No IP address tracking
- No session tracking
- No failed access attempts

**Required:**
```python
# Example: Comprehensive audit log entry
{
    "_id": ObjectId("..."),
    "event_type": "DELETE",
    "resource_type": "Book",
    "resource_id": "64f7b0a9f2a1e3c8b1234567",
    "user_id": "user_123",
    "user_email": "user@example.com",
    "ip_address": "192.168.1.1",
    "user_agent": "Mozilla/5.0...",
    "action": "soft_delete",
    "timestamp": "2025-01-24T10:00:00Z",
    "session_id": "session_xyz",
    "changes": {
        "before": {"deleted_at": null},
        "after": {"deleted_at": "2025-01-24T10:00:00Z", "deleted_by": "user_123"}
    },
    "result": "success",
    "compliance_flags": {
        "contains_pii": true,
        "retention_days": 90
    }
}
```

### 2. Data Retention Policy (HIGH)

**Gap:** No documented or enforced retention policy.

**SOC 2 Requirement:** PI1.5 - Must have documented retention periods.

**Current State:**
- Soft-deleted data remains indefinitely
- No automatic cleanup
- No policy documentation

**Required:**
- Document retention periods per data type
- Automate enforcement
- Audit compliance monthly

### 3. Permanent Deletion (HIGH)

**Gap:** No mechanism for permanent data disposal.

**SOC 2 Requirement:** PI1.4 - Data must be permanently deleted after retention period.

**Current State:**
- Soft-deleted data never permanently removed
- Violates "right to be forgotten" (GDPR)
- Storage costs increase indefinitely

**Required:**
- Automated permanent deletion job
- Manual override with audit trail
- Verification process

### 4. Access Controls & RBAC (HIGH)

**Gap:** No role-based access for sensitive operations.

**SOC 2 Requirement:** CC6.1 - Restrict access based on roles.

**Current State:**
- Basic user authentication via Firebase
- No distinction between regular users and admins
- Anyone can delete their own data
- No approval workflows for permanent deletion

**Required:**
```python
# Example: Role-based permissions
ROLES = {
    "user": ["read", "create", "update", "soft_delete_own"],
    "admin": ["read", "create", "update", "soft_delete_any", "restore"],
    "compliance_officer": ["read", "restore", "permanent_delete", "audit_log_read"],
    "super_admin": ["*"]  # All permissions
}
```

### 5. Compliance Reporting (MEDIUM)

**Gap:** No visibility into compliance status.

**SOC 2 Requirement:** CC4.1 - Management must monitor controls.

**Required Reports:**
- Data retention compliance report
- Pending deletions report
- Audit log summary
- Access control review report
- Incident report

---

## Implementation Roadmap

### Phase 1: Audit Logging (Weeks 1-2) - CRITICAL

**Deliverables:**
1. Create `AuditLog` model
2. Implement audit middleware/decorator
3. Log all CRUD operations on sensitive data
4. Add IP address and session tracking
5. Create audit log retention policy (7 years for SOC 2)

**Estimated Effort:** 3-5 days

### Phase 2: Data Retention Policy (Week 3) - HIGH

**Deliverables:**
1. Document retention periods per data type
2. Implement `DataRetentionPolicy` model
3. Create scheduled job for enforcement
4. Add configuration for retention periods
5. Create retention policy dashboard

**Estimated Effort:** 2-3 days

### Phase 3: Permanent Deletion (Week 4) - HIGH

**Deliverables:**
1. Implement safe permanent deletion service
2. Add pre-deletion verification checks
3. Create backup before permanent deletion
4. Add approval workflow for sensitive data
5. Implement cascade permanent deletion

**Estimated Effort:** 3-4 days

### Phase 4: Access Controls (Week 5) - HIGH

**Deliverables:**
1. Implement RBAC system
2. Define role permissions matrix
3. Add permission checks to endpoints
4. Create admin interface for role management
5. Add approval workflows

**Estimated Effort:** 4-5 days

### Phase 5: Compliance Reporting (Week 6) - MEDIUM

**Deliverables:**
1. Create compliance dashboard
2. Implement automated reports
3. Add alerting for policy violations
4. Create monthly compliance summary
5. Export capabilities for auditors

**Estimated Effort:** 3-4 days

### Phase 6: Testing & Documentation (Week 7) - MEDIUM

**Deliverables:**
1. Comprehensive testing of all features
2. Update API documentation
3. Create runbooks for operations
4. Prepare for external audit
5. Train team on compliance procedures

**Estimated Effort:** 3-5 days

**Total Implementation Time:** 6-7 weeks

---

## Technical Requirements

### 1. Audit Log Model

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Dict, Any, Literal

class AuditLog(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    # Event Information
    event_type: Literal["CREATE", "READ", "UPDATE", "DELETE", "RESTORE", "PERMANENT_DELETE", "LOGIN", "LOGOUT"]
    resource_type: str  # "Book", "Deck", "User", etc.
    resource_id: Optional[str] = None
    
    # Actor Information
    user_id: str
    user_email: Optional[str] = None
    user_role: Optional[str] = None
    
    # Context Information
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    
    # Action Details
    action: str  # "soft_delete", "restore", "create_book", etc.
    changes: Optional[Dict[str, Any]] = None  # Before/after state
    result: Literal["success", "failure", "partial"]
    error_message: Optional[str] = None
    
    # Compliance Metadata
    contains_pii: bool = False
    compliance_flags: Optional[Dict[str, Any]] = None
    retention_end_date: Optional[datetime] = None  # When this log can be deleted
    
    # Timestamp
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}
```

### 2. Data Retention Policy Model

```python
class DataRetentionPolicy(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    resource_type: str  # "Book", "Deck", "User", etc.
    retention_days: int  # Days to retain after soft delete
    permanent_delete_after: int  # Days after which permanent deletion occurs
    
    contains_pii: bool = False
    legal_basis: str  # "Contract", "Legitimate Interest", "Consent", etc.
    
    # GDPR/Privacy Flags
    subject_to_gdpr: bool = True
    subject_to_ccpa: bool = False
    allows_manual_deletion: bool = True  # User can request immediate deletion
    
    approval_required: bool = False  # Admin approval needed for permanent deletion
    backup_before_deletion: bool = True
    
    notes: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}
```

### 3. Audit Middleware/Decorator

```python
from functools import wraps
from fastapi import Request
import inspect

def audit_log(
    event_type: str,
    resource_type: str,
    contains_pii: bool = False
):
    """
    Decorator to automatically log operations.
    
    Usage:
        @audit_log(event_type="DELETE", resource_type="Book", contains_pii=True)
        async def delete_book(book_id: str, request: Request):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request and user from function arguments
            request = None
            current_user = None
            
            # Find Request and User in args/kwargs
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
            
            # Get user from Depends(get_current_user)
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                if param_name in kwargs:
                    if hasattr(kwargs[param_name], 'get'):
                        current_user = kwargs[param_name]
            
            # Execute the function
            result = None
            error = None
            try:
                result = await func(*args, **kwargs)
                status = "success"
            except Exception as e:
                error = str(e)
                status = "failure"
                raise
            finally:
                # Log the audit event
                await create_audit_log(
                    event_type=event_type,
                    resource_type=resource_type,
                    user_id=current_user.get("uid") if current_user else None,
                    user_email=current_user.get("email") if current_user else None,
                    ip_address=request.client.host if request else None,
                    user_agent=request.headers.get("user-agent") if request else None,
                    result=status,
                    error_message=error,
                    contains_pii=contains_pii
                )
            
            return result
        return wrapper
    return decorator

async def create_audit_log(
    event_type: str,
    resource_type: str,
    user_id: str,
    user_email: str,
    ip_address: str,
    user_agent: str,
    result: str,
    error_message: Optional[str] = None,
    contains_pii: bool = False,
    changes: Optional[Dict] = None,
    resource_id: Optional[str] = None
):
    """Create an audit log entry"""
    from app.database import db
    
    audit_entry = AuditLog(
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        user_email=user_email,
        ip_address=ip_address,
        user_agent=user_agent,
        result=result,
        error_message=error_message,
        contains_pii=contains_pii,
        changes=changes
    )
    
    await db["audit_logs"].insert_one(audit_entry.dict(by_alias=True))
```

### 4. Permanent Deletion Service

```python
from datetime import datetime, timedelta
from typing import List
import asyncio

class PermanentDeletionService:
    """
    Service to handle permanent deletion of soft-deleted records.
    Includes safety checks, backups, and audit trails.
    """
    
    @staticmethod
    async def find_expired_records(
        collection_name: str,
        retention_days: int
    ) -> List[dict]:
        """Find records that have exceeded retention period"""
        from app.database import db
        
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        expired = await db[collection_name].find({
            "deleted_at": {
                "$lt": cutoff_date,
                "$ne": None
            }
        }).to_list(None)
        
        return expired
    
    @staticmethod
    async def backup_before_deletion(
        collection_name: str,
        records: List[dict]
    ) -> str:
        """
        Create backup of records before permanent deletion.
        Returns backup_id for recovery if needed.
        """
        from app.database import db
        import uuid
        
        backup_id = str(uuid.uuid4())
        
        backup_doc = {
            "backup_id": backup_id,
            "collection_name": collection_name,
            "backup_date": datetime.utcnow(),
            "record_count": len(records),
            "records": records,
            "expires_at": datetime.utcnow() + timedelta(days=365)  # Keep backups 1 year
        }
        
        await db["deletion_backups"].insert_one(backup_doc)
        return backup_id
    
    @staticmethod
    async def permanent_delete_with_audit(
        collection_name: str,
        retention_days: int,
        user_id: str,
        dry_run: bool = False
    ) -> dict:
        """
        Permanently delete expired soft-deleted records.
        
        Args:
            collection_name: Name of the collection
            retention_days: Retention period in days
            user_id: User performing the deletion (admin)
            dry_run: If True, only report what would be deleted
            
        Returns:
            dict: Summary of deletion operation
        """
        from app.database import db
        
        # Find expired records
        expired_records = await PermanentDeletionService.find_expired_records(
            collection_name, retention_days
        )
        
        if not expired_records:
            return {
                "status": "success",
                "message": "No expired records found",
                "deleted_count": 0
            }
        
        if dry_run:
            return {
                "status": "dry_run",
                "message": f"Would delete {len(expired_records)} records",
                "expired_count": len(expired_records),
                "records": [str(r["_id"]) for r in expired_records]
            }
        
        # Create backup
        backup_id = await PermanentDeletionService.backup_before_deletion(
            collection_name, expired_records
        )
        
        # Permanent delete
        record_ids = [r["_id"] for r in expired_records]
        result = await db[collection_name].delete_many({
            "_id": {"$in": record_ids}
        })
        
        # Create audit log for permanent deletion
        await create_audit_log(
            event_type="PERMANENT_DELETE",
            resource_type=collection_name,
            user_id=user_id,
            user_email="system",
            ip_address="system",
            user_agent="automated_cleanup",
            result="success",
            contains_pii=True,
            changes={
                "deleted_count": result.deleted_count,
                "backup_id": backup_id,
                "retention_days": retention_days
            }
        )
        
        return {
            "status": "success",
            "message": f"Permanently deleted {result.deleted_count} records",
            "deleted_count": result.deleted_count,
            "backup_id": backup_id,
            "collection": collection_name
        }
```

### 5. Scheduled Cleanup Job

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

# Define retention policies
RETENTION_POLICIES = {
    "books": 90,           # 90 days
    "decks": 90,
    "study_cards": 90,
    "users": 365,          # 1 year for compliance
    "goals": 730,          # 2 years for historical analysis
    "focus_areas": 730,
    "priorities": 365,
    "audit_logs": 2555     # 7 years (SOC 2 requirement)
}

async def daily_cleanup_job():
    """
    Scheduled job to enforce data retention policies.
    Runs daily at 2 AM.
    """
    print(f"[{datetime.utcnow()}] Starting daily cleanup job")
    
    for collection_name, retention_days in RETENTION_POLICIES.items():
        try:
            result = await PermanentDeletionService.permanent_delete_with_audit(
                collection_name=collection_name,
                retention_days=retention_days,
                user_id="system",
                dry_run=False
            )
            print(f"  ✓ {collection_name}: {result['message']}")
        except Exception as e:
            print(f"  ✗ {collection_name}: Error - {str(e)}")
            # Alert administrators
            await send_alert(f"Cleanup job failed for {collection_name}: {str(e)}")
    
    print(f"[{datetime.utcnow()}] Cleanup job completed")

# Initialize scheduler
scheduler = AsyncIOScheduler()
scheduler.add_job(
    daily_cleanup_job,
    'cron',
    hour=2,
    minute=0,
    id='daily_cleanup'
)
scheduler.start()
```

---

## Data Retention Policy

### Standard Retention Periods

| Data Type | Soft Delete Retention | Permanent Delete After | Legal Basis |
|-----------|----------------------|----------------------|-------------|
| **User Accounts** | 365 days | 13 months | GDPR compliance |
| **Books (User Content)** | 90 days | 4 months | Contract fulfillment |
| **Decks & Study Cards** | 90 days | 4 months | Contract fulfillment |
| **Goals & Planning** | 730 days | 25 months | Legitimate interest |
| **Focus Areas** | 730 days | 25 months | Legitimate interest |
| **Priorities** | 365 days | 13 months | Contract fulfillment |
| **Audit Logs** | Never (7 years min) | 7+ years | Legal requirement (SOC 2) |
| **Deletion Backups** | 365 days | 13 months | Recovery capability |

### Exception Handling

**Legal Hold:**
- Data under legal hold must not be deleted
- Requires manual override by compliance officer
- Flag: `legal_hold: true` in document

**User Requests (GDPR Right to be Forgotten):**
- User can request immediate permanent deletion
- Must be completed within 30 days
- Requires identity verification
- Audit trail required

**Regulatory Requirements:**
- Financial data: 7 years minimum
- Health data (if applicable): 6 years minimum
- Audit logs: 7 years minimum (SOC 2)

---

## Audit Trail Requirements

### What Must Be Logged

#### 1. Authentication Events
- ✅ Login attempts (success/failure)
- ✅ Logout
- ✅ Password changes
- ✅ 2FA enable/disable
- ✅ API key creation/revocation

#### 2. Data Operations
- ✅ CREATE - New record creation
- ✅ READ - Access to sensitive data
- ✅ UPDATE - Data modifications
- ✅ DELETE - Soft deletion
- ✅ RESTORE - Data restoration
- ✅ PERMANENT_DELETE - Permanent deletion

#### 3. Administrative Actions
- ✅ User role changes
- ✅ Permission modifications
- ✅ System configuration changes
- ✅ Retention policy updates

### Audit Log Retention
- **Minimum:** 7 years (SOC 2 requirement)
- **Format:** Immutable, tamper-proof
- **Access:** Restricted to compliance officers
- **Backup:** Separate from production data

### Audit Log Security
```python
# Audit logs should be immutable
class AuditLog(BaseModel):
    # ... fields ...
    
    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError("Audit logs are immutable")
        super().__setattr__(name, value)
```

---

## Security Controls

### 1. Access Control Matrix

| Role | Read Own | Read Any | Create | Update Own | Update Any | Soft Delete Own | Soft Delete Any | Restore | Permanent Delete |
|------|----------|----------|--------|------------|------------|----------------|----------------|---------|------------------|
| **User** | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| **Admin** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| **Compliance Officer** | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **Super Admin** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

### 2. Data Encryption

**At Rest:**
- ✅ MongoDB Atlas encryption enabled
- ✅ Encryption key rotation every 90 days
- ✅ Separate keys for production/staging

**In Transit:**
- ✅ TLS 1.3 for all API connections
- ✅ Certificate pinning for mobile apps
- ✅ No unencrypted data transmission

### 3. Backup Security

**Backup Requirements:**
- ✅ Encrypted backups (AES-256)
- ✅ Stored in separate geographic region
- ✅ Access logged and monitored
- ✅ Regular restore testing (monthly)

---

## Monitoring & Reporting

### 1. Real-Time Monitoring

**Alerts for:**
- Unusual deletion patterns (>100 deletes/hour)
- Failed permanent deletion attempts
- Unauthorized access to deleted data
- Retention policy violations
- Audit log tampering attempts

```python
async def monitor_deletion_patterns():
    """Monitor for unusual deletion activity"""
    from app.database import db
    from datetime import datetime, timedelta
    
    # Check last hour
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    
    deletion_count = await db["audit_logs"].count_documents({
        "event_type": "DELETE",
        "timestamp": {"$gte": one_hour_ago}
    })
    
    if deletion_count > 100:
        await send_alert(
            severity="HIGH",
            message=f"Unusual deletion activity: {deletion_count} deletions in last hour",
            details={"threshold": 100, "actual": deletion_count}
        )
```

### 2. Compliance Reports

**Daily Report:**
- Deleted records count by type
- Pending permanent deletions
- Retention policy violations

**Weekly Report:**
- Access control violations
- Failed deletion attempts
- Audit log summary

**Monthly Report:**
- Compliance status overview
- Data retention metrics
- Security incidents summary
- Policy review reminders

**Quarterly Report:**
- Executive summary for audit
- Trend analysis
- Recommendations

### 3. Dashboard Metrics

```
┌─────────────────────────────────────────────┐
│         Compliance Dashboard                │
├─────────────────────────────────────────────┤
│ Soft Deleted Records:           1,234       │
│ Pending Permanent Deletion:       45        │
│ Overdue Deletions:                 3 ⚠️     │
│                                             │
│ Retention Policy Compliance:     98.5%      │
│ Audit Logs Captured:            99.9%       │
│                                             │
│ Last Cleanup Job:        2025-01-24 02:00   │
│ Next Cleanup Job:        2025-01-25 02:00   │
│                                             │
│ Recent Alerts:                     0        │
└─────────────────────────────────────────────┘
```

---

## Testing & Validation

### 1. Unit Tests

```python
import pytest
from datetime import datetime, timedelta

@pytest.mark.asyncio
async def test_soft_delete_creates_audit_log():
    """Verify soft delete creates audit log entry"""
    # Create test book
    book = await create_test_book()
    
    # Soft delete
    await soft_delete_book(book.id, user_id="test_user")
    
    # Verify audit log created
    audit_log = await db["audit_logs"].find_one({
        "resource_id": str(book.id),
        "event_type": "DELETE"
    })
    
    assert audit_log is not None
    assert audit_log["user_id"] == "test_user"
    assert audit_log["result"] == "success"

@pytest.mark.asyncio
async def test_retention_policy_enforcement():
    """Verify expired records are permanently deleted"""
    # Create soft-deleted record from 91 days ago
    old_date = datetime.utcnow() - timedelta(days=91)
    book = await create_test_book(deleted_at=old_date)
    
    # Run cleanup
    result = await PermanentDeletionService.permanent_delete_with_audit(
        "books", retention_days=90, user_id="system"
    )
    
    # Verify permanently deleted
    assert result["deleted_count"] == 1
    deleted_book = await db["books"].find_one({"_id": book.id})
    assert deleted_book is None
    
    # Verify backup exists
    backup = await db["deletion_backups"].find_one({
        "backup_id": result["backup_id"]
    })
    assert backup is not None

@pytest.mark.asyncio
async def test_restore_from_backup():
    """Verify records can be restored from backup"""
    # Create and delete book
    book = await create_test_book()
    await permanent_delete_book(book.id)
    
    # Restore from backup
    restored = await restore_from_backup(book.id, backup_id)
    
    assert restored.id == book.id
    assert restored.title == book.title
```

### 2. Integration Tests

```python
@pytest.mark.asyncio
async def test_full_deletion_lifecycle():
    """Test complete lifecycle: create -> soft delete -> permanent delete"""
    # 1. Create
    book = await create_book({"title": "Test Book"})
    assert book.deleted_at is None
    
    # 2. Soft delete
    await delete_book(book.id)
    book_after_soft = await db["books"].find_one({"_id": book.id})
    assert book_after_soft["deleted_at"] is not None
    
    # 3. Wait retention period (simulated)
    await simulate_time_passage(days=91)
    
    # 4. Permanent delete
    await run_cleanup_job()
    book_after_permanent = await db["books"].find_one({"_id": book.id})
    assert book_after_permanent is None
    
    # 5. Verify audit trail
    audit_logs = await get_audit_logs(resource_id=str(book.id))
    assert len(audit_logs) >= 3  # CREATE, DELETE, PERMANENT_DELETE
```

### 3. Compliance Validation

**Quarterly Audit Checklist:**
- [ ] All sensitive operations are logged
- [ ] Audit logs are immutable
- [ ] Retention policies are enforced
- [ ] Access controls are properly configured
- [ ] Backups are encrypted and tested
- [ ] Reports are generated automatically
- [ ] Alerts are functioning
- [ ] No unauthorized access attempts succeeded
- [ ] All permanent deletions have backups
- [ ] GDPR requests completed within 30 days

---

## Incident Response Plan

### 1. Data Breach Response

**If deleted data is accessed without authorization:**

1. **Immediate Actions (0-1 hour):**
   - Lock affected accounts
   - Revoke API keys
   - Enable enhanced logging
   - Alert security team

2. **Investigation (1-24 hours):**
   - Review audit logs
   - Identify scope of breach
   - Determine data accessed
   - Document timeline

3. **Remediation (24-72 hours):**
   - Patch vulnerability
   - Reset credentials
   - Notify affected users (if PII exposed)
   - File regulatory reports (if required)

4. **Post-Incident (1 week):**
   - Root cause analysis
   - Update security controls
   - Team training
   - Update incident response plan

### 2. Accidental Deletion Response

**If data is deleted in error:**

1. **Immediate:**
   - Restore from soft delete (if within retention period)
   - Restore from backup (if permanently deleted)

2. **Investigation:**
   - Review what triggered deletion
   - Check for system bugs
   - Verify user intent

3. **Prevention:**
   - Add confirmation prompts
   - Increase retention period if needed
   - Add "undo" functionality

---

## Next Steps

### Immediate Actions (This Week)
1. ✅ Review and approve this compliance document
2. ⏳ Create Jira tickets for Phase 1 implementation
3. ⏳ Schedule kickoff meeting with team
4. ⏳ Set up compliance Slack channel

### Short Term (This Month)
1. ⏳ Implement audit logging (Phase 1)
2. ⏳ Document retention policies (Phase 2)
3. ⏳ Set up basic monitoring

### Medium Term (Next 3 Months)
1. ⏳ Complete all 6 phases
2. ⏳ External security audit
3. ⏳ SOC 2 Type I assessment

### Long Term (6+ Months)
1. ⏳ SOC 2 Type II certification
2. ⏳ ISO 27001 consideration
3. ⏳ Continuous compliance program

---

## Appendix

### A. Glossary

**Soft Delete:** Marking data as deleted without physically removing it  
**Hard Delete:** Permanently removing data from the database  
**Retention Period:** Time data must be kept before permanent deletion  
**Audit Trail:** Chronological record of all system activities  
**PII:** Personally Identifiable Information  
**GDPR:** General Data Protection Regulation (EU)  
**SOC 2:** Service Organization Control 2 (security audit standard)

### B. References

- [SOC 2 Trust Services Criteria](https://www.aicpa.org/interestareas/frc/assuranceadvisoryservices/trustdataintegritytaskforce)
- [GDPR Data Retention Guidelines](https://gdpr-info.eu/)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)
- [ISO 27001 Standard](https://www.iso.org/isoiec-27001-information-security.html)

### C. Change Log

| Date | Version | Changes | Author |
|------|---------|---------|--------|
| 2025-01-24 | 1.0 | Initial document creation | System |

---

## Approval

This document requires approval from:

- [ ] **CTO/VP Engineering** - Technical feasibility
- [ ] **Security Officer** - Security controls
- [ ] **Compliance Officer** - Regulatory requirements
- [ ] **Legal** - Legal implications
- [ ] **Product Manager** - Business impact

**Status:** 📋 Pending Review  
**Target Approval Date:** [To be determined]  
**Implementation Start Date:** [After approval]

---

**Document Owner:** Engineering Team  
**Last Updated:** 2025-01-24  
**Next Review:** 2025-04-24 (Quarterly)
