"""
Outlook 提供者模块
"""

from .base import OutlookProvider, ProviderConfig
from .imap_new import IMAPNewProvider

__all__ = [
    'OutlookProvider',
    'ProviderConfig',
    'IMAPNewProvider',
]

PROVIDER_REGISTRY = {
    'imap_new': IMAPNewProvider,
}


def get_provider_class(provider_type: str):
    return PROVIDER_REGISTRY.get(provider_type)
