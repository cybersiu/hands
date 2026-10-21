"""
mcp_tasks.md — Three ten-action tasks to run through the MCP tools.

Each task is written as an ordered list of tool calls so it can be executed
step by step and verified at each point. The point is to exercise the tools
under real conditions, not to demo a happy path: every step names the evidence
that proves it worked.

TASK 1  YouTube: search, open a video, seek, pause, resume, switch, read metadata
TASK 2  Colab: open notebook, connect runtime, run all, read output, upload a file
TASK 3  Bing + Wikipedia: two searches, follow a result, read the destination
"""

# --------------------------------------------------------------------------
TASK_1 = """
TASK 1 — YouTube, ten actions
=============================

 1. switch_tab          find the YouTube tab, or new_tab to youtube.com
 2. observe             locate the search field by score (name=search_query)
 3. type_text           type "google colab"
 4. press               Enter
 5. assert_changed      location.href contains /results
 6. observe             list the result rows
 7. click               open the first video
 8. evaluate            read title, channel, views, duration from the player
 9. evaluate            set currentTime to 90, then read it back
10. evaluate            pause, read paused, play, read paused again

Evidence required: the URL changes twice, currentTime matches the seek target,
and paused flips true then false.
"""

# --------------------------------------------------------------------------
TASK_2 = """
TASK 2 — Colab, ten actions
===========================

 1. switch_tab          the Colab tab
 2. observe             confirm cells are present
 3. dismiss_blocker     clear any dialog covering the page
 4. evaluate            read the connect control's tooltip (runtime state)
 5. click               connect, if not already connected
 6. assert_changed      runtime state becomes connected
 7. click               Run all
 8. assert_changed      at least one cell reports an execution count
 9. evaluate            read each cell's output text
10. screenshot          capture the finished notebook

Evidence required: runtime goes disconnected -> connected, and at least one
cell shows a numeric execution count with non-empty output.
"""

# --------------------------------------------------------------------------
TASK_3 = """
TASK 3 — Bing and Wikipedia, ten actions
========================================

 1. switch_tab          any tab
 2. navigate            bing.com
 3. observe             find the search textarea (type=search, score 44)
 4. type_text           "python asyncio"
 5. press               Enter
 6. assert_changed      location.pathname contains /search
 7. evaluate            count result blocks
 8. navigate            wikipedia.org
 9. observe             find #searchInput by its Search Wikipedia label
10. type_text + press   "Alan Turing", Enter, then read the h1

Evidence required: both searches navigate, and Wikipedia's article heading is
non-empty.
"""
