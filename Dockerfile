# Pinned to the same version as the macOS Plex app used by the host runner (PLEX_VERSION in .env).
ARG PLEX_VERSION

# Container half of plex-ipbridge (real client addresses; see ipbridge/main.go)
FROM golang:1.27-alpine AS ipbridge
WORKDIR /src
COPY ipbridge/ .
RUN CGO_ENABLED=0 go build -trimpath -ldflags=-s -o /plex-ipbridge .

FROM plexinc/pms-docker:${PLEX_VERSION}
RUN mv "/usr/lib/plexmediaserver/Plex Transcoder" "/usr/lib/plexmediaserver/Plex Transcoder.real"
COPY --chmod=755 ["transcoder-stub.sh", "/usr/lib/plexmediaserver/Plex Transcoder"]
# iproute2: the ipbridge service installs one policy route at start
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
COPY --from=ipbridge /plex-ipbridge /usr/local/bin/plex-ipbridge
COPY --chmod=755 ipbridge/s6-run /etc/services.d/ipbridge/run
