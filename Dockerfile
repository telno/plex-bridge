# Pinned to the same version as the macOS Plex app used by the host runner (PLEX_VERSION in .env).
ARG PLEX_VERSION
FROM plexinc/pms-docker:${PLEX_VERSION}
RUN mv "/usr/lib/plexmediaserver/Plex Transcoder" "/usr/lib/plexmediaserver/Plex Transcoder.real"
COPY --chmod=755 ["transcoder-stub.sh", "/usr/lib/plexmediaserver/Plex Transcoder"]
