# Copyright (c) Microsoft. All rights reserved.
"""LangGraph tools for the Hisense TV Sports AI Assistant POC."""

from tools.foundry_iq import foundry_iq_search
from tools.memory import recall_viewer_preferences, remember_viewer_preference
from tools.scenario import get_live_scores, query_schedule, tune_to_channel
from tools.webiq import webiq_search

ALL_TOOLS = [
    foundry_iq_search,
    webiq_search,
    query_schedule,
    get_live_scores,
    tune_to_channel,
    remember_viewer_preference,
    recall_viewer_preferences,
]

__all__ = [
    "ALL_TOOLS",
    "foundry_iq_search",
    "webiq_search",
    "query_schedule",
    "get_live_scores",
    "tune_to_channel",
    "remember_viewer_preference",
    "recall_viewer_preferences",
]
