# -*- coding: utf-8 -*-
"""Shared mutable runtime state (replaces reassigned globals).

These three values were previously module-level globals in bot.py that got
reassigned at runtime (`global ...`). After splitting into modules a plain
global would only rebind the name in one module, so they live here on a shared
object instead.
"""
import os


class _RuntimeState:
    nsgifts_client = None
    chatgpt_webapp_enabled = os.getenv("CHATGPT_WEBAPP_ENABLED", "1") == "1"
    claude_webapp_enabled = os.getenv("CLAUDE_WEBAPP_ENABLED", "1") == "1"


rt = _RuntimeState()
