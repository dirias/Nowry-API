#!/bin/bash

# Documentation Organization Script
# Moves scattered .md files into proper docs/ structure

set -e

echo ""
echo "============================================================"
echo "📁 NOWRY DOCUMENTATION ORGANIZATION"
echo "============================================================"
echo ""

# Base directories
NOWRY_ROOT="/Users/CVYV0H267P-didier/Nowry"
API_DOCS="$NOWRY_ROOT/Nowry-API/docs"
FRONTEND_DOCS="$NOWRY_ROOT/nowry/docs"

# Create docs structure if not exists
mkdir -p "$API_DOCS"/{features,technical,operations,guides}
mkdir -p "$FRONTEND_DOCS"/{components,guides}

echo "📂 Creating documentation structure..."
echo "   ✓ API docs: $API_DOCS"
echo "   ✓ Frontend docs: $FRONTEND_DOCS"
echo ""

# Move API documentation files
echo "📦 Organizing API documentation..."

# Features
mv "$NOWRY_ROOT/Nowry-API/PUBLIC_CONTENT_FEATURE.md" "$API_DOCS/features/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/PUBLIC_CONTENT_SETUP.md" "$API_DOCS/features/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/PAGE_MODEL_DEPRECATION.md" "$API_DOCS/features/" 2>/dev/null || true

# Technical
mv "$NOWRY_ROOT/Nowry-API/CASCADE_DELETION_COMPLETE.md" "$API_DOCS/technical/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/CASCADE_DELETION_STATUS.md" "$API_DOCS/technical/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/DELETION_IMPLEMENTATION.md" "$API_DOCS/technical/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/DELETION_BEHAVIOR.md" "$API_DOCS/technical/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/SOFT_DELETE_GUIDE.md" "$API_DOCS/technical/" 2>/dev/null || true

# Operations
mv "$NOWRY_ROOT/Nowry-API/SOC2_COMPLIANCE.md" "$API_DOCS/operations/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/MVP_READY.md" "$API_DOCS/operations/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/IMPLEMENTATION_CHECKLIST.md" "$API_DOCS/operations/" 2>/dev/null || true

# Guides
mv "$NOWRY_ROOT/Nowry-API/SEED_TEST_USER_README.md" "$API_DOCS/guides/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/SEED_COMPLETE.md" "$API_DOCS/guides/" 2>/dev/null || true
mv "$NOWRY_ROOT/Nowry-API/SEED_QUICK_REF.md" "$API_DOCS/guides/" 2>/dev/null || true

echo "   ✓ Moved API documentation files"

# Move Frontend documentation files
echo "📦 Organizing Frontend documentation..."

# Components
mv "$NOWRY_ROOT/nowry/DELETE_MODAL_USAGE_GUIDE.md" "$FRONTEND_DOCS/components/" 2>/dev/null || true
mv "$NOWRY_ROOT/nowry/PUBLIC_ACCESS_POINTS.md" "$FRONTEND_DOCS/components/" 2>/dev/null || true

echo "   ✓ Moved Frontend documentation files"

# Move root-level files
echo "📦 Organizing root-level documentation..."

mv "$NOWRY_ROOT/CASCADE_DELETION_SUMMARY.md" "$API_DOCS/technical/" 2>/dev/null || true
mv "$NOWRY_ROOT/IMPLEMENTATION_COMPLETE.md" "$API_DOCS/operations/" 2>/dev/null || true

echo "   ✓ Moved root-level documentation files"

# Create index files
echo ""
echo "📝 Creating index files..."

# API docs index
cat > "$API_DOCS/README.md" << 'EOF'
# Nowry API Documentation

## 📚 Documentation Structure

### Features (`/features`)
Documentation about API features and functionality:
- `PUBLIC_CONTENT_FEATURE.md` - Public content sharing system
- `PUBLIC_CONTENT_SETUP.md` - Setup guide for public content
- `PAGE_MODEL_DEPRECATION.md` - Migration from Page model

### Technical (`/technical`)
Technical implementation details:
- `CASCADE_DELETION_COMPLETE.md` - Cascade deletion implementation
- `CASCADE_DELETION_STATUS.md` - Deletion status and analysis
- `DELETION_IMPLEMENTATION.md` - Soft delete implementation
- `DELETION_BEHAVIOR.md` - Deletion behavior documentation
- `SOFT_DELETE_GUIDE.md` - Guide to using soft delete
- `CASCADE_DELETION_SUMMARY.md` - Summary of cascade deletion

### Operations (`/operations`)
Operational guides and checklists:
- `SOC2_COMPLIANCE.md` - SOC 2 compliance documentation
- `MVP_READY.md` - MVP readiness checklist
- `IMPLEMENTATION_CHECKLIST.md` - Implementation tracking
- `IMPLEMENTATION_COMPLETE.md` - Implementation completion summary

### Guides (`/guides`)
User and developer guides:
- `SEED_TEST_USER_README.md` - Test user seed script documentation
- `SEED_COMPLETE.md` - Seed script completion summary
- `SEED_QUICK_REF.md` - Quick reference for seed script

---

## 🚀 Quick Links

### For Developers
- [Seed Test User Guide](./guides/SEED_TEST_USER_README.md)
- [Soft Delete Guide](./technical/SOFT_DELETE_GUIDE.md)
- [Cascade Deletion](./technical/CASCADE_DELETION_COMPLETE.md)

### For Admins
- [SOC 2 Compliance](./operations/SOC2_COMPLIANCE.md)
- [MVP Checklist](./operations/MVP_READY.md)

### For Features
- [Public Content Sharing](./features/PUBLIC_CONTENT_FEATURE.md)
- [Public Content Setup](./features/PUBLIC_CONTENT_SETUP.md)
EOF

echo "   ✓ Created API docs README"

# Frontend docs index  
cat > "$FRONTEND_DOCS/components/README.md" << 'EOF'
# Frontend Component Documentation

## Component Guides

### Common Components
- `DELETE_MODAL_USAGE_GUIDE.md` - DeleteConfirmationModal usage and examples
- `PUBLIC_ACCESS_POINTS.md` - Public content access points in UI

---

## Quick Reference

### DeleteConfirmationModal
Beautiful confirmation modal for deletions with clear consequences.

See [DELETE_MODAL_USAGE_GUIDE.md](./DELETE_MODAL_USAGE_GUIDE.md) for usage.

### Public Content
User access points for discovering and interacting with public content.

See [PUBLIC_ACCESS_POINTS.md](./PUBLIC_ACCESS_POINTS.md) for details.
EOF

echo "   ✓ Created Frontend docs README"

# Update main docs README
cat > "$NOWRY_ROOT/nowry/docs/README.md" << 'EOF'
# Nowry Documentation

Welcome to the Nowry documentation! This directory contains all documentation for the Nowry learning platform.

## 📂 Documentation Structure

### Design (`/design`)
Design system and UI/UX guidelines:
- `DESIGN_GUIDELINES.md` - Complete design system
- `COLOR_SYSTEM.md` - Dynamic color generation

### Features (`/features`)
Feature documentation and implementation guides:
- Annual Planning system
- Bug Reporting system
- Study System implementation
- Text-to-Speech features
- Multi-column layouts
- Slash commands

### Technical (`/technical`)
Technical architecture and implementation:
- API refactoring
- Content-first architecture
- Pagination redesign
- User management
- Development standards
- Import alignment

### Deploy (`/deploy`)
Deployment and infrastructure:
- Deployment guides
- Infrastructure documentation
- Environment variables
- Quick deploy scripts

### Planning (`/planning`)
Product planning and roadmaps:
- Subscription system plans
- Landing page enhancements
- Auth and onboarding

### Tasks (`/tasks`)
Task tracking and roadmaps:
- Implementation roadmap
- Documentation organization

### Components (`/components`)
UI component documentation:
- Component usage guides
- Public access points
- Modal patterns

---

## 🚀 Quick Start

### For Designers
Start with [Design Guidelines](./design/DESIGN_GUIDELINES.md) and [Color System](./design/COLOR_SYSTEM.md).

### For Developers
- [Development Standards](./technical/DEVELOPMENT_STANDARDS.md)
- [API Refactor Guide](./technical/API_REFACTOR_COMPLETE.md)
- [Content-First Architecture](./technical/CONTENT_FIRST_ARCHITECTURE.md)

### For Deployment
- [Deployment Guide](./deploy/DEPLOYMENT_GUIDE.md)
- [Quick Deploy](./deploy/QUICK_DEPLOY.md)
- [Environment Setup](./deploy/ENVIRONMENT_VARIABLES.md)

### For Features
Browse the [features](./features/) directory for specific feature documentation.

---

## 📝 Contributing

When adding new documentation:
1. Choose the appropriate directory
2. Use descriptive ALL_CAPS_SNAKE_CASE.md filenames
3. Include a clear title and table of contents
4. Update this README with a link

---

## 🔗 External Resources

- [API Documentation](../../Nowry-API/docs/README.md)
- [GitHub Repository](https://github.com/your-org/nowry)
EOF

echo "   ✓ Updated main docs README"

echo ""
echo "============================================================"
echo "✅ DOCUMENTATION ORGANIZATION COMPLETE!"
echo "============================================================"
echo ""
echo "📁 Documentation is now organized in:"
echo "   📂 $API_DOCS"
echo "   📂 $FRONTEND_DOCS"
echo ""
echo "📝 Index files created:"
echo "   ✓ $API_DOCS/README.md"
echo "   ✓ $FRONTEND_DOCS/components/README.md"
echo "   ✓ $NOWRY_ROOT/nowry/docs/README.md"
echo ""
echo "============================================================"
