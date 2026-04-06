from __future__ import annotations
from typing import Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class VoiceSideSettings(BaseModel):
    voice_name: Optional[str] = None
    voice_lang: Optional[str] = None
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.0, le=2.0)
    auto_play: bool = False


class VoiceSettings(BaseModel):
    front: VoiceSideSettings = Field(default_factory=VoiceSideSettings)
    back: VoiceSideSettings = Field(default_factory=VoiceSideSettings)


class StudyConfig(BaseModel):
    pace_mode: Literal["relaxed", "balanced", "intensive"] = "balanced"
    new_per_day: Optional[int] = Field(default=None, ge=1, le=500)
    max_reviews_per_day: Optional[int] = Field(default=None, ge=1, le=1000)


class PublicMetadataInput(BaseModel):
    description: Optional[str] = Field(default=None, max_length=280)


class DeckSettingsUpdate(BaseModel):
    voice_settings: Optional[VoiceSettings] = None
    config: Optional[StudyConfig] = None
    is_public: Optional[bool] = None
    public_metadata: Optional[PublicMetadataInput] = None


class DeckSettingsResponse(BaseModel):
    id: str
    voice_settings: VoiceSettings
    config: StudyConfig
    is_public: bool
    published_at: Optional[datetime] = None
    public_metadata: Optional[PublicMetadataInput] = None
    updated_at: datetime
