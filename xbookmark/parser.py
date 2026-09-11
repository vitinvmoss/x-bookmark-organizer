"""GraphQL parsing facade. Real logic in tweet_extract.py + pages.py."""
from __future__ import annotations
from .tweet_extract import extract_tweet, unwrap_result
from .pages import parse_bookmarks_page, parse_folder_list

__all__ = ["extract_tweet", "unwrap_result",
           "parse_bookmarks_page", "parse_folder_list"]
