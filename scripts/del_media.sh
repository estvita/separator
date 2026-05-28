#!/bin/sh

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DIR="$PROJECT_DIR/separator/media/temp"

[ -d "$DIR" ] || exit 0

find "$DIR" -type f -mmin +60 -delete
