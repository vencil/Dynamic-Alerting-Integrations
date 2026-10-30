#!/bin/sh
# go_build.sh — the ONE place this repository's `go build` flags are written
# (#3425). Every build of a Go binary goes through it: the images (da-tools'
# build.sh, the tenant-api and recipe-preview Dockerfiles), the release
# workflow's air-gapped binaries, the CI workflows, the Makefile's *-build
# targets, the pytest fixtures (tests/_go_build.py builds the argv) and the
# Go tests that build a da-guard. tests/ops/test_go_build_ssot.py turns red
# on any other place that runs `go build`.
#
#   sh scripts/ops/go_build.sh [-X importpath.name=value]... \
#       [--goos OS] [--goarch ARCH] <module-dir> <package> <output>
#
#   <module-dir>  the directory holding the module's go.mod (the build runs
#                 there); relative to the caller's directory
#   <package>     the package, relative to <module-dir> (`./cmd/da-guard`, `.`)
#   <output>      the binary to write; relative to the caller's directory
#   -X            a linker string variable, e.g. `-X main.Version=v1.2.3`
#                 (repeatable; the value must not contain whitespace)
#   --goos/--goarch  cross-compile target (default: the host's, or $GOOS/$GOARCH)
#
# Why each flag is here, and why none of them is optional:
#
#   CGO_ENABLED=0  Not a portability nicety. A cgo Go runtime starts every OS
#                  thread with pthread_create, and each thread's stack is an
#                  RLIMIT_STACK-sized (typically 8 MiB) private mapping that
#                  counts against RLIMIT_DATA. The generator service judges
#                  under RLIMIT_DATA (192 MiB by default) and runs da-guard,
#                  da-read and da-crdecode there: a cgo build measured
#                  112-152 MiB of VmData on a tree where the static build stays
#                  at 71-76 MiB, and in CI it aborted with
#                  `runtime/cgo: pthread_create failed` (#3174, #3206). It also
#                  makes the binary static, so the distroless / alpine images
#                  and the cross-compiled arm64 builds need no libc or QEMU.
#   -trimpath      Drops the builder's absolute paths from the binary: the
#                  directory the build ran in (a laptop, a CI runner, a
#                  worktree) is not in the bytes or in a stack trace, whose
#                  source paths start at the module path instead.
#   -ldflags -s -w Drops the symbol table and the DWARF debug info: the
#                  binaries ship in images and release archives, where nobody
#                  attaches a debugger; with -trimpath this takes da-tools'
#                  five binaries (linux/amd64) from 148.5 MB to 105.5 MB
#                  uncompressed, 64.8 MB to 33.4 MB after gzip -9. Stack traces
#                  still work (they use the runtime's pclntab, which -s -w
#                  keeps), and `go version -m` still reads the build settings.
#   -buildvcs=false  The build must not depend on a .git being readable: a
#                  worktree's .git file can name a path the dev container
#                  cannot see, and the Docker build contexts carry no .git.
#
# Written for POSIX sh, not bash: the golang:*-alpine builder stages run it
# with busybox ash.
set -eu

usage() {
    echo "usage: $0 [-X importpath.name=value]... [--goos OS] [--goarch ARCH] <module-dir> <package> <output>" >&2
    exit 2
}

ldflags="-s -w"
while [ "$#" -gt 0 ]; do
    case "$1" in
        -X)
            [ "$#" -ge 2 ] || usage
            case "$2" in
                *[[:space:]]*|'')
                    echo "$0: -X value must be non-empty and contain no whitespace: '$2'" >&2
                    exit 2 ;;
                *=*) ;;
                *)
                    echo "$0: -X value must be importpath.name=value: '$2'" >&2
                    exit 2 ;;
            esac
            ldflags="$ldflags -X $2"
            shift 2 ;;
        --goos)
            [ "$#" -ge 2 ] || usage
            GOOS="$2"; export GOOS
            shift 2 ;;
        --goarch)
            [ "$#" -ge 2 ] || usage
            GOARCH="$2"; export GOARCH
            shift 2 ;;
        --) shift; break ;;
        -*) echo "$0: unknown option: $1" >&2; usage ;;
        *) break ;;
    esac
done
[ "$#" -eq 3 ] || usage

module_dir=$1
package=$2
output=$3
case "$output" in
    /*) ;;
    *) output="$PWD/$output" ;;
esac

cd "$module_dir"
CGO_ENABLED=0
export CGO_ENABLED
exec go build -buildvcs=false -trimpath -ldflags "$ldflags" -o "$output" "$package"
