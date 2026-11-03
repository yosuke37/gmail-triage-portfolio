#!/bin/sh
set -eu
# The app owns its database but has no cluster administration privileges.
# The generated URL-safe password is sent over stdin, never in psql arguments.
app_password=$(cat /run/secrets/postgres_password)
case "$app_password" in
  ''|*[!A-Za-z0-9_-]*) echo "Invalid local application password format" >&2; exit 1 ;;
esac
printf "CREATE ROLE triage LOGIN PASSWORD '%s' NOSUPERUSER NOCREATEDB NOCREATEROLE; ALTER DATABASE triage OWNER TO triage;\n" "$app_password" |
  psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1
unset app_password
