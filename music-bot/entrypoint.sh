#!/bin/sh
# Update yt-dlp on every start: YouTube changes often, fresh yt-dlp fixes most breakage.
pip install --user --quiet --upgrade "yt-dlp[default]" || echo "yt-dlp update skipped"
exec python bot.py
