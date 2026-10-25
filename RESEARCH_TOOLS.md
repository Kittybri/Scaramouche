# Research tools v1 (opt-in)

These commands add bounded read-only research without changing AI personalities, memories, voice, rivalry, Google integrations or message handling. No external tools are executed as shell commands.

## Commands

Scaramouche uses the scara prefix, Wanderer uses the wan prefix.

- !scararesearch youtube https://youtu.be/VIDEO_ID
- !scararesearch web https://example.org/article
- !scararesearch github owner/repo
- !scararesearch github owner/repo:path/to/file.py
- !scararesearch rss https://example.org/feed.xml
- !scarafeed latest https://example.org/feed.xml
- !scarafeed add https://example.org/feed.xml
- !scarafeed list
- !scarafeed remove https://example.org/feed.xml

For Wanderer, replace scara with wan. Existing commands and conversational behavior are unchanged.

YouTube uses only a public transcript excerpt; videos without public transcripts are unsupported. It does not watch video frames or hear spoken audio directly. Some cloud IPs may be throttled or blocked by YouTube. GitHub uses its public REST API and has unauthenticated rate limits. No private repository access, GitHub tokens or write actions. Full-page reading extracts text from regular HTML; JavaScript-only sites can remain inaccessible. It is not a headless browser.

## Feed announcements

Feed subscriptions are per channel and only members with Manage Channels can add/remove. Adding a feed saves its current newest entry, avoiding a flood of historical articles. The 30-minute polling loop is **disabled by default**. Set RESEARCH_FEED_POLL_ENABLED=1 on Oracle only after testing and choosing announcement channels. The subscriber's account must have Send Messages permission in the chosen channel.

At most five feeds per channel and 80 feeds per bot are supported. The SQLite store defaults to MEMORY_DATA_DIR, then BOT_DATA_DIR, then /data if present, otherwise the current directory. Use persistent Oracle volume storage. No separate scheduler or extra server is required.

## Safety and operations

Outbound requests are limited to HTTPS port 443, with resolved non-public IPs rejected, each redirect revalidated, bounded response sizes and timeouts. Sources are treated as untrusted data rather than new instructions. This significantly reduces risks but does not constitute a security audit; outbound HTTP/TLS and source formatting still warrant production testing.

No provider API keys are required for the basic tools. New optional dependency: youtube-transcript-api, and XML parser: defusedxml.

Run: python -m unittest discover -s tests -p 'test_research_sources.py'


## Natural-language research

In addition to prefixed commands, explicit research requests addressed to the bot trigger the appropriate read-only tool. Examples:

- Wanderer, explain https://www.youtube.com/watch?v=VIDEO_ID
- Scara, review the GitHub repo octocat/Hello-World
- Wanderer, summarize https://example.org/news
- Scaramouche, search for Genshin patch notes
- Wanderer, check the latest RSS feed at https://example.org/feed.xml

In a server, explicitly address the character by its name, @mention it, or reply to its message. Direct messages are also supported. Messages from bots, ordinary chatter, requests beginning with !, unsafe URLs, and messages explicitly saying not to research are skipped. The existing partner-bot gating, muted users, owner-only mode and banned-channel controls run before this integration.

Only one URL is handled per request. A per-user 30-second rate limit and a two-request concurrency cap protect AI and source providers. The tool uses bounded source excerpts and reports citations with all Discord mentions disabled. Responses are stored in the existing memory database, not raw external webpage text. Existing character dialogue generation is used for summaries; characters retain their distinct personalities.

Natural-language web search reuses the existing public search snippets and does not claim full article access. The bot-specific prefixed commands remain available as a more explicit fallback.
