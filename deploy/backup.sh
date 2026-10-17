#!/bin/sh
# Copy the live database with sqlite3 .backup and keep the newest 7 files.
set -eu

db=/var/lib/latam-jobs/latam-jobs.sqlite
dest=/var/lib/latam-jobs/backups

if [ ! -f "$db" ]; then
    echo "No database at $db" >&2
    exit 1
fi

mkdir -p "$dest"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sqlite3 "$db" ".backup '$dest/latam-jobs-$stamp.sqlite'"

find "$dest" -type f -name 'latam-jobs-*.sqlite' -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR > 7 { print $2 }' \
    | xargs -r rm -f
