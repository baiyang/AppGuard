#!/bin/sh
set -eu

usage() {
    printf '%s\n' \
        'Usage: sh scripts/build.sh ISSUER_KEY PUBLIC_KEY CODE_KEY [IMAGE]' \
        '' \
        'Build the Flask example using existing AppGuard keys.' \
        'IMAGE defaults to example-web:001.' \
        'APPGUARD_PLATFORM defaults to linux/amd64.' \
        'Key paths are relative to the directory where this command is run.'
}

case "${1-}" in
    -h|--help) usage; exit 0 ;;
esac

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    usage >&2
    exit 2
fi

issuer_key=$1
publisher_public=$2
code_key=$3
image=${4:-example-web:001}
platform=${APPGUARD_PLATFORM:-linux/amd64}

for key in "$issuer_key" "$publisher_public" "$code_key"; do
    if [ ! -f "$key" ] || [ ! -r "$key" ]; then
        printf 'Key file is not a readable regular file: %s\n' "$key" >&2
        exit 2
    fi
done

example_dir=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)

set -x
exec docker buildx build --load --platform "$platform" \
    --file "$example_dir/Dockerfile" --tag "$image" \
    --no-cache-filter protected-build \
    --build-context "application=$example_dir" \
    --secret "id=issuer_key,src=$issuer_key" \
    --secret "id=publisher_public,src=$publisher_public" \
    --secret "id=code_key,src=$code_key" \
    "$example_dir"
