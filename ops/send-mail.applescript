-- usage: osascript ops/send-mail.applescript <subject> <body-file> <to> <from> [<styles-file>]
-- Sends a message through Mail.app using the account already configured there (no credentials are stored in this project).
-- With a styles file (written by ops/render-report.py) the body is sent as formatted rich text; each line is
--   paragraph <tab> from <tab> to <tab> font <tab> size <tab> RRGGBB or "-"      (from/to 0 = the whole paragraph)
-- A style that cannot be applied is skipped, so the worst case is a plainer message, never a lost one.
-- (Mail's "html content" property is not used: current macOS sends such messages with an empty body.)
on run argv
	set subj to item 1 of argv
	set bodyText to read (POSIX file (item 2 of argv)) as «class utf8»
	set toAddr to item 3 of argv
	set fromAddr to item 4 of argv
	set styleLines to {}
	if (count of argv) > 4 then
		try
			set styleLines to paragraphs of (read (POSIX file (item 5 of argv)) as «class utf8»)
		end try
	end if
	set applied to 0
	set oldDelims to AppleScript's text item delimiters
	set AppleScript's text item delimiters to tab
	tell application "Mail"
		set m to make new outgoing message with properties {subject:subj, content:bodyText, visible:false, sender:fromAddr}
		tell m to make new to recipient at end of to recipients with properties {address:toAddr}
		repeat with ln in styleLines
			try
				set f to text items of (ln as text)
				if (count of f) is 6 then
					set p to (item 1 of f) as integer
					set a to (item 2 of f) as integer
					set b to (item 3 of f) as integer
					tell content of m
						if a is 0 then
							set font of paragraph p to item 4 of f
							set size of paragraph p to (item 5 of f) as integer
							if item 6 of f is not "-" then set color of paragraph p to my rgb(item 6 of f)
						else
							set font of characters a thru b of paragraph p to item 4 of f
							set size of characters a thru b of paragraph p to (item 5 of f) as integer
						end if
					end tell
					set applied to applied + 1
				end if
			end try
		end repeat
		set AppleScript's text item delimiters to oldDelims
		send m
	end tell
	if applied > 0 then return "sent (formatted)"
	return "sent (plain)"
end run

on rgb(hex)
	set digits to "0123456789ABCDEF"
	set out to {}
	repeat with i from 1 to 5 by 2
		set hi to (offset of (character i of hex) in digits) - 1
		set lo to (offset of (character (i + 1) of hex) in digits) - 1
		set end of out to (hi * 16 + lo) * 257
	end repeat
	return out
end rgb
