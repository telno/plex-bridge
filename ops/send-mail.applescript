-- usage: osascript ops/send-mail.applescript <subject> <body-file> <to> <from>
-- Sends a plain-text message through Mail.app using the account already configured there (no credentials are stored in this project).
on run argv
	set subj to item 1 of argv
	set bodyFile to item 2 of argv
	set toAddr to item 3 of argv
	set fromAddr to item 4 of argv
	set bodyText to read (POSIX file bodyFile) as «class utf8»
	tell application "Mail"
		set m to make new outgoing message with properties {subject:subj, content:bodyText, visible:false, sender:fromAddr}
		tell m to make new to recipient at end of to recipients with properties {address:toAddr}
		send m
	end tell
	return "sent"
end run
