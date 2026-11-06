-- "Open in mdfeed" — the Finder side of `mdview`.
--
-- Why an AppleScript app and not the shell script it wraps: Finder's Open With
-- list is built from LaunchServices, which only registers app bundles, so a
-- chmod +x script never appears in it. And a bundle whose executable is a shell
-- script does not help — Finder delivers the file as an `odoc` Apple Event, not
-- as arguments, so such an app launches with an empty argv and no idea what it
-- was asked to open. `on open` is the handler that receives that event.
--
-- The two placeholders are substituted with absolute paths at install time
-- (`just install-finder-app`), because an app launched by Finder inherits
-- launchd's PATH — /usr/bin:/bin:/usr/sbin:/sbin. That has no ~/bin for mdview,
-- and no uv, which mdview needs for mdfeed.py's `uv run --script` shebang.

on open theFiles
	-- One mdview for the whole selection, not one per file. Each run probes for
	-- a viewer and opens a tab if it finds none, so runs back to back race: the
	-- first is still starting a server when the next looks, and each can end
	-- up with a tab of its own. Given several files, mdview lands on the last.
	set args to ""
	repeat with f in theFiles
		set args to args & " " & quoted form of POSIX path of f
	end repeat
	try
		do shell script "export PATH=" & quoted form of "__PATH__" & ":$PATH; " & ¬
			quoted form of "__MDVIEW__" & args
	on error errText number errNum
		-- -128 is the user cancelling; anything else is worth surfacing,
		-- since a droplet has no window to print to.
		if errNum is not -128 then
			display notification errText with title "Open in mdfeed"
		end if
	end try
end open

on run
	-- Opened with no document — double-clicked in Finder, or launched from the
	-- Dock. There is nothing to show.
	display notification "Open a .md file with this app, or run mdview from a shell." with title "Open in mdfeed"
end run
