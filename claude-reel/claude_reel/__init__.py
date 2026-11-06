"""claude-reel — a Claude Code session rendered as a paced, collapsed slideshow.

Pipeline: extract (transcript -> reel of beats) -> curate (beats -> slides) ->
render (slides -> self-contained HTML deck). The reel is the pivot format: code or an
LLM can curate it, and any renderer can consume it.
"""

__version__ = "0.1.0"
