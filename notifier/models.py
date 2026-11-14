import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Notification(Model):
    event_id: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9_.:@/-]+$')
    target_code: str = Field(min_length=2, max_length=40, pattern=r'^[a-z][a-z0-9_-]+$')
    title: str = Field(default='', max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    expires_in_seconds: int = Field(default=86400, ge=60, le=604800, strict=True)

    @field_validator('body', 'title')
    @classmethod
    def clean_text(cls, text):
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', text):
            raise ValueError('消息含不支持的控制字符')
        return text

    @field_validator('body')
    @classmethod
    def not_blank(cls, value):
        if not value:
            raise ValueError('请填写消息内容')
        return value


class Search(Model):
    query: str = Field(min_length=1, max_length=100)
    recipient_type: Literal['group', 'private'] = 'group'
    allow_contact_lookup: StrictBool = False

    @field_validator('query')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('请填写搜索内容')
        if re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError('搜索内容不能包含控制字符')
        return value.strip()


class Bind(Model):
    candidate_id: str = Field(min_length=16, max_length=80)
    code: str = Field(min_length=2, max_length=40, pattern=r'^[a-z][a-z0-9_-]+$')
    label: str = Field(min_length=1, max_length=60)
    confirmed_visual: StrictBool = False

    @field_validator('label')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('请填写显示名称')
        return value.strip()


class BackgroundCheck(Model):
    verify_layout: StrictBool = False
    query: str = Field(default='', max_length=100, pattern=r'^[^\x00-\x1f\x7f]*$')


class Settings(Model):
    dispatch_enabled: StrictBool | None = None
    interval_seconds: int | None = Field(default=None, ge=5, le=3600, strict=True)
    max_pending: int | None = Field(default=None, ge=1, le=5000, strict=True)


class NativeConnection(Model):
    base_url: str = Field(min_length=10, max_length=100)
    token: str = Field(default='', max_length=256)


class Retry(Model):
    checked_not_sent: StrictBool = False
