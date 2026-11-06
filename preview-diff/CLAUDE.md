# preview-diff

## Browser tests and motion

`test_review_ui.py` asserts scroll positions. The page turns off smooth scrolling
only under `prefers-reduced-motion`, so the `kb_page` fixture asks for it —
otherwise every assertion races an animation.

The trap is using it everywhere. Comment hopping (`n`/`p`) derived its cursor from
scroll position, which drops hops at ordinary key-repeat speed with motion on;
every hop test used `kb_page`, so the suite could not see it.
`test_hopping_survives_smooth_scrolling` deliberately uses the plain `page`
fixture. A new keyboard test that reads a position wants one of each.

Every fixture diff except `TALL_DIFF` renders shorter than the viewport. On those
nothing scrolls, and a positional assertion passes without testing anything.
