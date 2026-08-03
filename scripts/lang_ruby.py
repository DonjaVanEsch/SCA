"""
Ruby-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_ruby").

Required exports:
    LANGUAGE_ID   - str
    REGISTRY_FILE - str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool
"""

import json
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "ruby"
REGISTRY_FILE = "registry ruby.json"


class RubyGemsLookupError(Exception):
    """Raised when a rubygems.org fetch fails for a network/rate-limit
    reason -- deliberately distinct from _resolve() returning None for a
    gem/version actually checked and confirmed absent. Same bug class as
    Rust's CratesIoLookupError/PHP's PackagistLookupError/Java's
    MavenLookupError/.NET's NuGetLookupError/Node's NpmLookupError/
    Python's PyPiLookupError: conflating the two used to make
    write_context() delete existing output on a transient failure
    (confirmed live for Java: a run during sustained Maven Central 429s
    wiped every Java image context on disk). Callers must not delete
    existing output on this exception."""


# ── rubygems.org version resolution ─────────────────────────────────────────
# rubygems.org's /api/v1/versions/{gem}.json is the direct analog of
# Packagist's p2 metadata / Maven's maven-metadata.xml: it lists every
# published version, each carrying its own "prerelease" boolean, "platform"
# string and "created_at"/"ruby_version" fields directly -- no hyphen-based
# prerelease sniffing needed the way PHP/.NET have to do, RubyGems' own API
# already tells us. Like Composer/Maven/NuGet and unlike npm, Bundler's
# `gem "x", "1.2.3"` pins an EXACT version per `bundle install` (no runtime
# range-resolution the way npm does) -- so the same "bake the resolved
# version into a small generated constants file, don't bother reading it
# back at runtime" reasoning applies here too (see _versions_rb() below).

_GEM_VERSIONS: dict = {}
_GEM_RELEASE_DATES: dict = {}
_GEM_RUBY_FLOOR: dict = {}   # gem -> {version: ruby_version constraint str}


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    except ValueError:
        return (0,)


def _fetch_gem_versions(gem: str) -> list:
    """Raises RubyGemsLookupError on a network/rate-limit failure -- does
    NOT cache that as "zero versions found" (see RubyGemsLookupError's
    docstring)."""
    if gem in _GEM_VERSIONS:
        return _GEM_VERSIONS[gem]

    url = f"https://rubygems.org/api/v1/versions/{gem}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        # Native-extension gems (bcrypt, argon2) publish separate entries
        # per platform (e.g. "x86_64-linux", "arm64-darwin") sharing the
        # SAME version number as the "ruby" source gem -- Bundler picks the
        # right platform variant automatically at install time from a
        # single `gem "x", "1.2.3"` pin, so only the platform-independent
        # "ruby" entry is needed here to know which version NUMBERS exist.
        entries = [e for e in data if e.get("platform", "ruby") == "ruby" and not e.get("prerelease", False)]
        versions = sorted({e["number"] for e in entries}, key=_ver_key)
        _GEM_RELEASE_DATES[gem] = {e["number"]: e["created_at"][:10] for e in entries if e.get("created_at")}
        _GEM_RUBY_FLOOR[gem] = {e["number"]: e.get("ruby_version") for e in entries}
    except (URLError, OSError, ValueError, KeyError) as exc:
        raise RubyGemsLookupError(f"{gem}: {exc}") from exc

    _GEM_VERSIONS[gem] = versions
    return versions


def _release_date(gem: str, version: str) -> str | None:
    """release_date for one already-known version -- reuses
    _fetch_gem_versions()'s cache, no extra request."""
    try:
        _fetch_gem_versions(gem)
    except RubyGemsLookupError:
        return None
    return _GEM_RELEASE_DATES.get(gem, {}).get(version)


def _resolve(gem: str, registry_ver: str) -> str | None:
    """Resolve a registry bucket like '7' or '3.0' to the latest matching
    stable release on rubygems.org (e.g. '7' -> '7.2.201', '3.0' -> '3.0.7'
    but NOT '3.1.x')."""
    versions = _fetch_gem_versions(gem)

    prefix = registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in versions:
        return registry_ver

    return None


def _ruby_floor_tuple(constraint: str | None) -> tuple:
    """Extract the numeric floor out of a RubyGems `ruby_version` constraint
    string (e.g. '>= 2.3.0' -> (2, 3, 0), '~> 2.3' -> (2, 3)). A None/blank
    constraint (old gemspecs never declared one) means "no restriction"."""
    if not constraint:
        return (0,)
    m = re.search(r"\d+(?:\.\d+)*", constraint)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(0).split("."))


def _lang_ver_tuple(lang_ver: str) -> tuple:
    return tuple(int(x) for x in lang_ver.split("."))


def _era_gem_version(gem_name: str, lang_ver: str, fallback: str) -> str:
    """Pick the newest {gem_name} release whose own declared ruby_version
    floor is satisfied by lang_ver, live-verified against rubygems.org's
    own per-version metadata (the same "ruby_version" field this module
    already reads for every other gem) rather than a hardcoded guess.

    Shared by every "this transitive dependency floats to whatever's
    newest overall and eventually outpaces an older Ruby bucket" case in
    this module (Bundler itself, Rake via argon2's ffi-compiler,
    concurrent-ruby/multi_json via Rails' activesupport/i18n, ...) --
    each such gem keeps raising its own Ruby floor release over release,
    so a single hardcoded pin would eventually go stale; this always
    resolves the newest one that's ACTUALLY compatible with lang_ver.
    """
    lv = _lang_ver_tuple(lang_ver)
    try:
        versions = _fetch_gem_versions(gem_name)
    except RubyGemsLookupError:
        return fallback

    floors = _GEM_RUBY_FLOOR.get(gem_name, {})
    for v in sorted(versions, key=_ver_key, reverse=True):
        floor = _ruby_floor_tuple(floors.get(v))
        if lv >= floor:
            return v
    return fallback


def _bundler_version(lang_ver: str) -> str:
    """Bundler is NOT preinstalled at all in the old ruby:1.9/2.0/2.1-slim
    Docker images (Bundler only became a RubyGems "default gem" bundled
    with the interpreter itself starting around Ruby 2.6) -- every combo
    on an old Ruby bucket needs an explicit `gem install bundler` of a
    COMPATIBLE Bundler version before `bundle install` can run at all.
    Bundler 2.x itself keeps raising its own Ruby floor release over
    release (e.g. required >=2.3 for a while, then >=2.6), so even a
    "just pin some old Bundler 1.x" fallback would be wrong for the
    2.3-2.5 bucket range -- hence _era_gem_version(), not a hardcoded
    cutoff table. 1.17.3 is known to work all the way back to Ruby 1.8.7,
    a safe (if not maximally current) fallback.
    """
    return _era_gem_version("bundler", lang_ver, "1.17.3")


def _bundler_version_1x(lang_ver: str) -> str:
    """Like _bundler_version() but capped to the Bundler 1.x line, for
    frameworks that declare their own 'bundler ~> 1.0' constraint (Rails
    major 3, resolved to 3.2.22.5) -- confirmed via a real failing build
    ("Because rails >= 3.0.3, < 4.0.0.beta1 depends on bundler ~> 1.0
    and the current Bundler version (2.4.22) does not satisfy ...").
    """
    lv = _lang_ver_tuple(lang_ver)
    try:
        versions = [v for v in _fetch_gem_versions("bundler") if v.startswith("1.")]
    except RubyGemsLookupError:
        return "1.17.3"

    floors = _GEM_RUBY_FLOOR.get("bundler", {})
    for v in sorted(versions, key=_ver_key, reverse=True):
        floor = _ruby_floor_tuple(floors.get(v))
        if lv >= floor:
            return v
    return "1.17.3"


def _rake_version(lang_ver: str) -> str:
    """Only needed for argon2: its own dependency ffi-compiler declares
    `s.add_dependency 'rake'` completely UNCONSTRAINED (confirmed via its
    real gemspec) -- left to float, Bundler resolves whatever's newest
    overall, breaking on any older Ruby bucket once that newest release's
    own floor outpaces it. Confirmed via a real failing build (Ruby 2.1 +
    Rails 3 + argon2: "rake-13.4.2 requires ruby version >= 2.3, which is
    incompatible with the current version, ruby 2.1.10").
    """
    return _era_gem_version("rake", lang_ver, "12.3.3")


def _concurrent_ruby_version(lang_ver: str) -> str:
    """Rails' own activesupport pulls in concurrent-ruby as a transitive
    dependency (thread-safety primitives) with no useful upper bound of
    its own, and concurrent-ruby's later 1.2+/1.3+ releases raised their
    floor to >=2.3 -- confirmed via a real failing build (Ruby 2.1 +
    Rails 3 + argon2, right after fixing the same class of issue for
    rake: "concurrent-ruby-1.3.8 requires ruby version >= 2.3, which is
    incompatible with the current version, ruby 2.1.10"). Applied
    whenever Rails is used, any major -- harmless on newer Ruby buckets
    where the natural unconstrained pick would already land here.
    """
    return _era_gem_version("concurrent-ruby", lang_ver, "1.1.10")


# ── Framework metadata ───────────────────────────────────────────────────────

_FW_PACKAGE: dict = {
    "Rails":   "rails",
    "Sinatra": "sinatra",
    "Grape":   "grape",
    "Hanami":  "hanami",
    "Roda":    "roda",
    "Padrino": "padrino",
}

# Which (framework, major) combos genuinely resolve Rack >=3 -- and
# therefore need the SEPARATE `rackup` gem (Rack 3.0 extracted the
# `rackup` command/Rack::Server/Rack::Handler out of the `rack` gem
# itself into github.com/rack/rackup -- confirmed via rack's own
# CHANGELOG.md: "Extract `rackup` command, `Rack::Server`, `Rack::Handler`
# and related code into a separate gem."). Every value below was
# confirmed by reading each framework's OWN gemspec at a real tag, not
# assumed:
#   - Rails 3/4/5/6 pin rack tightly to the 1.x/2.x line each was built
#     against (actionpack.gemspec: 3.2="~> 1.4.5", 4.2="~> 1.6",
#     5.2/6.1="~> 2.0") -- never resolves Rack 3.
#   - Rails 7/8 (this registry resolves each bucket to its LATEST patch)
#     declare only a lower bound (7.1.5/8.0.2 actionpack.gemspec:
#     "rack, >= 2.2.4", no upper bound) -- Bundler's default
#     newest-compatible-first resolution picks Rack 3.x here.
#   - Sinatra 1/2/3 pin rack 1.x/2.x (gemspec: 1.4="~> 1.5", 2.0="~> 2.0",
#     3.x="~> 2.2, >= 2.2.4"); Sinatra 4 requires "rack, >= 3.0.0, < 4"
#     and does NOT itself depend on the `rackup` gem (confirmed: its own
#     Sinatra::Base#run! prints "bundle add rackup puma" and exits when
#     Rackup::Handler isn't defined) -- must be added ourselves.
#   - Grape 1.x: an earlier pass found grape.gemspec excluding Rack 3
#     ("rack, >= 1.3.0, < 3") -- the LATEST patch this bucket resolves to
#     (1.8.0) has since dropped that gemspec ceiling, but its own SOURCE
#     (lib/grape.rb) still `require`s 'rack/auth/digest/md5', a file
#     Rack 3.0 deleted outright -- confirmed via a real crash (LoadError)
#     once Rack 3 got resolved. Now explicitly pinned to Rack "~> 2.2" in
#     make_gemfile() instead (same real-incompatibility-not-fixable-via-
#     gemspec-alone class as Grape bucket 0's own Rack pin) -- so it
#     still never needs the separate `rackup` gem. Grape 2.x/3.x relax
#     to "rack, >= 2" (no upper bound) and both only support Ruby
#     >=2.7/>=3.3 respectively (already >= Rack 3's own >=2.4.0 Ruby
#     floor) -- always resolves Rack 3, and don't share bucket 1's
#     rack/auth/digest/md5 require (confirmed: neither 2.x's nor 3.x's
#     lib/grape.rb references it).
#   - Hanami 1.x/2.x pin hanami-router (and thus rack) to "~> 2.0"
#     (confirmed via hanami-router's own gemspec at v1.3.2 and v2.0.0);
#     Hanami 3.x's hanami-router relaxes to "rack, >= 2.2.16" (no upper
#     bound) with a Ruby floor of >=3.3 -- always resolves Rack 3.
#   - Padrino's padrino-core.gemspec (0.16.1, the real released version)
#     directly depends on both "sinatra, ~> 4" and "rackup, ~> 2.1" --
#     always Rack 3, and `rackup` is already pulled in transitively, but
#     adding it explicitly too is harmless and keeps this table uniform.
_ALWAYS_RACKUP = {("Sinatra", "4"), ("Grape", "2"), ("Grape", "3"),
                  ("Hanami", "3"), ("Padrino", "0"),
                  ("Rails", "7"), ("Rails", "8")}
_NEVER_RACKUP = {("Sinatra", "1"), ("Sinatra", "2"), ("Sinatra", "3"),
                 ("Grape", "0"), ("Grape", "1"),
                 ("Hanami", "0"), ("Hanami", "1"), ("Hanami", "2"),
                 ("Rails", "3"), ("Rails", "4"), ("Rails", "5"), ("Rails", "6")}
# Grape bucket 0 now gets Rack explicitly pinned to "~> 1.6" in
# make_gemfile() (a real Rack 3/2.1+ incompatibility, see its own
# comment there) -- always pre-Rack-3, so it never needs `rackup`,
# moved out of the ruby-version-dependent set below.
#
# Roda 3.x ("rack" with NO version constraint at all, confirmed via
# roda.gemspec) leaves Rack fully up to whatever else is in the bundle
# -- since nothing else pins it, Bundler resolves the newest Rack
# compatible with the INSTALLED RUBY ITSELF, so whether Rack 3 (Ruby
# floor >=2.4.0, confirmed via rack.gemspec at v3.0.0) is reachable
# depends on lang_ver, not on the framework major.
_RUBY_DEPENDENT_RACKUP = {("Roda", "3")}


def _needs_rackup(fw_name: str, fw_major: str, lang_ver: str) -> bool:
    key = (fw_name, fw_major)
    if key in _ALWAYS_RACKUP:
        return True
    if key in _NEVER_RACKUP:
        return False
    if key in _RUBY_DEPENDENT_RACKUP:
        return _lang_ver_tuple(lang_ver) >= (2, 4)
    return False


# pqc_rails's own gemspec (confirmed via its README) hard-requires a REAL
# Rails host application: "actionpack"/"activerecord"/"railties", each
# constrained ">=7.1, <9". This project's images/<lang>/.../<framework>/
# <fw_ver>/<lib_name>/<lib_ver> structure otherwise treats the framework
# and crypto-library axes as fully independent, so this cross-dependency
# (already flagged in the registry's own pqc_rails notes) has to be
# enforced here explicitly -- the same kind of real, structural
# framework/library incompatibility as PHP's Laravel-4 x phpseclib skip,
# just keyed by a rule (any non-Rails-7/8 framework) rather than a fixed
# tuple set, since it applies broadly rather than to one specific pairing.
def _pqc_rails_needs_skip(fw_name: str, fw_major: str) -> bool:
    return not (fw_name == "Rails" and fw_major in ("7", "8"))


# argon2 + Rails on Ruby <3.1 is a genuine, extensively-confirmed
# impossibility, not a convenience exclusion. Rails' own transitive
# dependency graph (rake, concurrent-ruby, multi_json, rack-cache, and
# for major 3 specifically its own 'bundler ~> 1.0' gemspec constraint)
# is all individually pinnable to era-appropriate versions -- but
# argon2's own 'ffi ~> 1.9' dependency resolves to a PLATFORM-SPECIFIC
# precompiled variant (e.g. ffi-1.17.4-x86_64-linux-musl) whose own
# floor is Ruby >=3.0, which no Gemfile version pin can work around
# (confirmed: pinning an older ffi version number doesn't change which
# platform variant Bundler selects; `bundle config set
# force_ruby_platform true` doesn't help either, at least not with the
# old Bundler 1.x this Ruby range needs for other reasons). Confirmed
# clean on Ruby 3.1 for Rails major 3; Rails major 4 hit a DIFFERENT
# unconstrained gem (minitest, floor >=3.1) at the exact same boundary
# -- every Rails major's own transitive graph keeps surfacing a new
# blocker below this line, so the boundary is applied to Rails as a
# whole rather than re-diagnosing each major individually.
def _argon2_rails_needs_skip(fw_name: str, fw_major: str, lang_ver: str) -> bool:
    return fw_name == "Rails" and _lang_ver_tuple(lang_ver) < (3, 1)


# ── Crypto library metadata ──────────────────────────────────────────────────
# "touch" mirrors every other lang_X.py's LIB_META convention: a real call
# into the library so it's provably loaded and exercised, not just
# declared as a Gemfile dependency. Every API shape below was confirmed
# against each gem's own real README/source on rubygems.org/GitHub (see
# this module's accompanying research notes in the PR/commit message),
# not guessed.

LIB_META: dict = {
    "openssl": {
        "imports": 'require "openssl"',
        # OpenSSL 3.0+ (this registry's own notes) disallows the old
        # no-arg '{DH,DSA,EC,RSA}.new' constructor forms -- HMAC.hexdigest
        # never used that shape at any point, so it's safe across every
        # tracked bucket (2 through 4).
        "touch": 'OpenSSL::HMAC.hexdigest("SHA256", "pqc-sca-probe-key", "pqc-sca probe")',
    },
    "rbnacl": {
        "imports": 'require "rbnacl"',
        "touch": (
            "rbnacl_signing_key = RbNaCl::SigningKey.generate\n"
            'rbnacl_signing_key.sign("pqc-sca probe")'
        ),
    },
    "bcrypt": {
        "imports": 'require "bcrypt"',
        "touch": 'BCrypt::Password.create("pqc-sca probe")',
    },
    "jwt": {
        "imports": 'require "jwt"',
        # 3-arg JWT.encode(payload, secret, alg) confirmed working
        # unchanged all the way back to jwt-0.1.8 (2011) via its own
        # README at that tag -- safe across every tracked bucket (0-3).
        "touch": 'JWT.encode({ probe: "pqc-sca" }, "pqc-sca-secret", "HS256")',
    },
    "argon2": {
        "imports": 'require "argon2"',
        # Only valid for buckets 1/2 -- bucket 0's real API predates
        # `.create` entirely, see _argon2_touch() below.
        "touch": 'Argon2::Password.create("pqc-sca probe")',
    },
    "digest": {
        "imports": 'require "digest"',
        "touch": 'Digest::SHA256.hexdigest("pqc-sca probe")',
    },
    "roqs": {
        "imports": 'require "roqs"',
        # Defensive try/rescue, matching this project's own convention for
        # every other young liboqs binding (PHP's php-liboqs, etc.):
        # 'Kyber768' is roqs' own README example algorithm name (liboqs
        # keeps the legacy Kyber alias alongside the NIST ML-KEM name for
        # backward compatibility); wrapped defensively since alg
        # availability depends on the liboqs build's own compile-time
        # algorithm list, not just roqs' own (very stable, single-digit
        # release count) FFI layer.
        "touch": (
            "begin\n"
            '  roqs_kem = Roqs::KEM.new("Kyber768")\n'
            "  roqs_pub, roqs_sec = roqs_kem.genkeypair\n"
            "rescue StandardError\n"
            "  # exercised; ignore init failure (alg availability depends on\n"
            "  # the liboqs build's own compile-time algorithm list)\n"
            "end"
        ),
    },
    "jwt-pq": {
        "imports": 'require "jwt"\nrequire "jwt/pq"',
        "touch": (
            "begin\n"
            "  jwt_pq_key = JWT::PQ::Key.generate(:ml_dsa_65)\n"
            '  JWT.encode({ probe: "pqc-sca" }, jwt_pq_key, "ML-DSA-65")\n'
            "rescue StandardError\n"
            "  # exercised; ignore init failure (very young gem/liboqs pairing)\n"
            "end"
        ),
    },
    "pqc_rails": {
        "imports": 'require "pqc_rails"',
        "touch": (
            "begin\n"
            "  PqcRails::Kem.open(:ml_kem_512) { |kem| kem.generate_keypair }\n"
            "rescue StandardError\n"
            "  # exercised; ignore init failure (very young gem/liboqs pairing)\n"
            "end"
        ),
    },
}

# Every crypto lib name in this registry equals its own RubyGems package
# name (unlike PHP's paragonie/phpseclib/secudoc-namespaced packages) --
# no separate name-mapping dict needed.
_LIB_GEM = {name: name for name in LIB_META}


def _argon2_touch(lib_ver: str) -> str:
    """argon2 bucket 0 (resolves to its final patch, 0.1.4) predates the
    `Argon2::Password.create` class method entirely -- confirmed by
    reading lib/argon2.rb straight out of the real 0.1.4/1.0.0 gems:
    0.1.4 only exposes `.hash(pass)`/`.verify_password`, `.create` was
    added starting at 1.0.0. Found via a real failing build (NoMethodError:
    undefined method 'create' for Argon2::Password:Class, Ruby 2.2 + Grape
    bucket 0 + argon2 bucket 0)."""
    if lib_ver == "0":
        return 'Argon2::Password.hash("pqc-sca probe")'
    return 'Argon2::Password.create("pqc-sca probe")'


# roqs' own lib/roqs/struct.rb uses Fiddle (not FFI) for its C struct
# definitions, including a bare (non-pointer) 'uint8_t claimed_nist_level'
# field -- Ruby's Fiddle::CParser#parse_ctype never learned to recognize
# the 8-bit fixed-width C99 type names (only int64_t/uint64_t get a
# regex case, confirmed by reading fiddle/cparser.rb directly), so this
# crashes with Fiddle::DLError: unknown type: uint8_t -- confirmed via a
# real crash on Ruby 2.6/2.7. Patching Fiddle::CParser#parse_ctype
# ourselves (delegating everything except the missing type name to the
# real implementation) before requiring roqs fixes it -- confirmed live
# that roqs loads correctly with this patch in place. The Gemfile (see
# make_gemfile()) separately adds 'fiddle' as an explicit gem on Ruby
# 4.0+, where it was dropped from Ruby's own default gems (same class
# of change as ostruct elsewhere in this module) -- this function only
# needs to require it there too, harmless as a plain require elsewhere
# since it's already bundled by default on every older Ruby.
def _roqs_imports() -> str:
    lines = [
        'require "fiddle"',
        'require "fiddle/cparser"',
        "module Fiddle",
        "  module CParser",
        "    alias_method :_pqc_orig_parse_ctype, :parse_ctype",
        "    def parse_ctype(ty, tymap = nil)",
        '      if ty.to_s =~ /^u?int8_t(?:\\s+\\w+)?$/',
        "        return ty.to_s.start_with?('u') ? -TYPE_CHAR : TYPE_CHAR",
        "      end",
        "      _pqc_orig_parse_ctype(ty, tymap)",
        "    end",
        "  end",
        "end",
        'require "roqs"',
    ]
    return "\n".join(lines)


# rbnacl is a pure-FFI binding (confirmed via its own gemspec: only
# depends on 'ffi', no C-extension compile of its own) needing the real
# libsodium shared library present as a SYSTEM (not build) dependency.
# roqs/jwt-pq/pqc_rails all need the real liboqs C library built from
# source (see _LIBOQS_TAG below) -- a build-time-only cost once
# liboqs.so exists. Every combo now goes through the same multi-stage
# builder (see make_dockerfile()) since a C compiler turned out to be
# needed far more broadly than just these libraries' own native
# extensions -- see builder_apt's own comment there.
_NEEDS_LIBSODIUM = {"rbnacl"}
_NEEDS_LIBOQS    = {"roqs", "jwt-pq", "pqc_rails"}


# ── Pre-fetch ────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from rubygems.org for every gem this run
    needs, plus Bundler's own version list (needed by _bundler_version())."""
    gems: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        pkg = _FW_PACKAGE.get(fw["name"])
        if pkg:
            gems.add(pkg)
    for lib in lang_data.get("cryptography_libs", []):
        pkg = _LIB_GEM.get(lib["name"])
        if pkg:
            gems.add(pkg)
    gems.add("bundler")

    print("Fetching available versions from rubygems.org ...")
    for gem in sorted(gems):
        try:
            versions = _fetch_gem_versions(gem)
            print(f"  {gem}: {len(versions)} version(s) found")
        except RubyGemsLookupError as exc:
            print(f"  [WARN] {exc}")
    print()


# ── Docker image / Debian-archive helpers ────────────────────────────────────
# jessie (2.1/2.2), stretch (2.3), buster (2.4/2.5) are the live-verified
# EOL Debian codenames needing the archive.debian.org redirect already
# established for Go/Node/PHP's own old bases in this project. 2.6+
# resolves to bullseye or later, still live on deb.debian.org.
#
# NOTE: the registry's own '_comment_docker_repo' claims 2.1 is wheezy --
# that was wrong. Confirmed live via 'docker run ruby:2.1-slim cat
# /etc/os-release': it's actually jessie already (libc6/apt/dpkg are all
# jessie-era, 2.19-18+deb8u10). Pointing its apt sources at wheezy instead
# made apt try to pull in wheezy-era gcc-4.7/libc6-dev against the
# already-installed jessie libc6, an unresolvable conflict that would have
# removed 113 core packages (apt/dpkg/bash included) -- confirmed via a
# real failing build ("unmet dependencies ... held broken packages").
# 1.9/2.0 are excluded outright (schema-1 base images, unpullable), so
# their real codename no longer matters.

_ARCHIVE_CODENAME_BY_VER = {
    "2.1": "jessie",
    "2.2": "jessie",
    "2.3": "stretch",
    "2.4": "buster",
    "2.5": "buster",
}


# libsodium's own Debian package name is tied to its SONAME, which has
# bumped across these codenames -- confirmed live via `apt-cache search
# libsodium` in each: jessie ships 1.0.0 as libsodium13, stretch ships
# libsodium18, buster and every later codename (bullseye/bookworm/trixie,
# confirmed on ruby:2.6-slim) ship libsodium23. Hardcoding "libsodium23"
# unconditionally (as this module used to) is a real bug on jessie/
# stretch: "E: Unable to locate package libsodium23", confirmed via a
# real failing build.
_LIBSODIUM_PKG_BY_CODENAME = {
    "jessie": "libsodium13",
    "stretch": "libsodium18",
}


def _libsodium_pkg(ruby_ver: str) -> str:
    codename = _ARCHIVE_CODENAME_BY_VER.get(ruby_ver)
    return _LIBSODIUM_PKG_BY_CODENAME.get(codename, "libsodium23")


def _debian_archive_apt(ruby_ver: str) -> tuple:
    codename = _ARCHIVE_CODENAME_BY_VER.get(ruby_ver)
    apt_sources = (
        f"RUN echo 'deb http://archive.debian.org/debian {codename} main' > /etc/apt/sources.list \\\n"
        f"    && echo 'deb http://archive.debian.org/debian-security {codename}/updates main' >> /etc/apt/sources.list\n"
        if codename else ""
    )
    apt_flag     = "-o Acquire::Check-Valid-Until=false " if codename else ""
    allow_unauth = "--allow-unauthenticated "              if codename else ""
    return apt_sources, apt_flag, allow_unauth


# liboqs 0.16.0 (2026-07-09) is the current real stable tag -- confirmed
# live via GitHub's releases API (open-quantum-safe/liboqs), NOT assumed
# to still be the "0.15.0" pin used elsewhere in this project for
# php-liboqs (that pin predates this module; 0.16.0 had only existed as
# an -rc1 prerelease at that time). Also exactly matches the LIBOQS_VERSION
# jwt-pq's own extconf.rb vendors when building its own copy from source,
# confirmed by reading that file directly on GitHub.
_LIBOQS_TAG = "0.16.0"


def _liboqs_build_stage() -> str:
    return (
        f"RUN git clone --depth 1 --branch {_LIBOQS_TAG} \\\n"
        "    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \\\n"
        "    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \\\n"
        "       -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \\\n"
        "       -DOQS_BUILD_ONLY_LIB=ON -GNinja \\\n"
        "    && cmake --build /tmp/liboqs/build --target install \\\n"
        "    && rm -rf /tmp/liboqs && ldconfig\n"
    )


# ── App templates ─────────────────────────────────────────────────────────────

_VERSION_OBJ_RB = """{
  language: { name: "Ruby", version: RUBY_VERSION },
  framework: { name: "__FW_NAME__", version: FRAMEWORK_VERSION },
  library: { name: "__LIB_NAME__", version: LIBRARY_VERSION },
}"""


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


def _versions_rb(fw_resolved: str, lib_resolved: str) -> str:
    return (
        f'FRAMEWORK_VERSION = "{fw_resolved}"\n'
        f'LIBRARY_VERSION = "{lib_resolved}"\n'
    )


# Sinatra's classic top-level `get "/" do ... end` DSL is confirmed
# unchanged all the way back to 0.9.6 (2010, the last 0.x release before
# 1.0 -- verified via that tag's own README.rdoc) through the current 4.x
# -- a single template covers every tracked major (1-4). Sinatra 4
# additionally needs the separate `rackup` gem (see _needs_rackup()); it's
# still invoked the same self-running way (`ruby app.rb`, NOT `rackup`)
# since Sinatra's own Base#run! do the Rack::Handler/Rackup::Handler
# lookup internally once the gem is present.
_SINATRA_APP = """\
require "sinatra"
require "json"
require_relative "versions"
__LIB_IMPORTS__

set :bind, "0.0.0.0"
set :port, 8000
# See make_dockerfile()'s own comment on WEBrick's default reverse-DNS
# lookup stall -- confirmed real all the way back to Sinatra 1.4.8's
# identical run!() (server_settings merged straight into the handler's
# own options).
set :server_settings, { DoNotReverseLookup: true }

__LIB_TOUCH__

get "/" do
  content_type :json
  { message: "Hello World" }.to_json
end

get "/version" do
  content_type :json
  __VERSION_OBJ__.to_json
end
"""

# Grape's `class API < Grape::API` + `get "/" do ... end` subclass DSL is
# confirmed unchanged from v0.2.0 (2011, verified via that tag's own
# README.markdown) through the current 3.x/4.0-dev -- a single template
# covers every tracked major (0-3). Runs via `bundle exec rackup`
# (Grape::API is a plain Rack app, no self-running mode of its own).
_GRAPE_CONFIG_RU = """\
require "grape"
require "json"
require_relative "versions"
__LIB_IMPORTS__

__LIB_TOUCH__

class App < Grape::API
  format :json

  get "/" do
    { message: "Hello World" }
  end

  get "/version" do
    __VERSION_OBJ__
  end
end

run App
"""

# Roda's `class App < Roda; route do |r| ... end; end` routing-tree DSL,
# confirmed via the gem's own current README.rdoc ("Running the
# Application" section shows this exact shape run via `rackup`). Single
# long-lived major (bucket "3") -- one template.
_RODA_CONFIG_RU = """\
require "roda"
require "json"
require_relative "versions"
__LIB_IMPORTS__

__LIB_TOUCH__

class App < Roda
  route do |r|
    r.root do
      response["Content-Type"] = "application/json"
      { message: "Hello World" }.to_json
    end

    r.get "version" do
      response["Content-Type"] = "application/json"
      __VERSION_OBJ__.to_json
    end
  end
end

run App
"""

# Padrino::Application < Sinatra::Base (confirmed via padrino-core's own
# lib/padrino-core/application.rb source) -- a Padrino app is a Sinatra
# modular-style subclass registered with Padrino's own setup/routing/
# params-protection modules. Single long-lived major (bucket "0").
_PADRINO_CONFIG_RU = """\
require "padrino-core"
require "json"
require_relative "versions"
__LIB_IMPORTS__

__LIB_TOUCH__

class App < Padrino::Application
  get "/" do
    content_type :json
    { message: "Hello World" }.to_json
  end

  get "/version" do
    content_type :json
    __VERSION_OBJ__.to_json
  end
end

run App
"""

# Rails: hand-written Rails::Application subclass (option (b) from this
# module's own design brief) rather than a full `rails new` scaffold --
# lighter, no scaffolding-CLI dependency, and structurally identical to
# Rails' OWN official minimal single-file reproduction template
# (rails/rails guides/bug_report_templates/action_controller.rb, fetched
# and confirmed directly from the rails/rails repo). Every era difference
# is resolved at RUNTIME via `config.respond_to?(...)` guards instead of
# per-major Python branching, so one template covers every tracked major
# (3-8):
#   - config.load_defaults/config.api_only/config.hosts were each added
#     at a different Rails version (5.0, 5.0, 6.0 respectively) -- guarded
#     so the same file works unmodified on Rails 3 through 8.
#   - config.secret_key_base was added at Rails 4; Rails 3 instead uses
#     config.secret_token -- both guarded, whichever exists gets set.
#   - config.api_only = true reproduces the real `rails new --api` lighter
#     middleware stack this registry's own notes call out for Rails 5+.
_RAILS_CONFIG_RU = """\
require "action_controller/railtie"
require_relative "versions"
__LIB_IMPORTS__

class TestApp < Rails::Application
  config.root = __dir__
  config.eager_load = false
  config.logger = Logger.new($stdout)
  config.load_defaults Rails::VERSION::STRING.to_f if config.respond_to?(:load_defaults)
  config.api_only = true if config.respond_to?(:api_only=)
  config.hosts.clear if config.respond_to?(:hosts)
  config.secret_key_base = "pqc-sca-probe-secret-key-base-0000000000" if config.respond_to?(:secret_key_base=)
  config.secret_token = "pqc-sca-probe-secret-token-0000000000000000" if config.respond_to?(:secret_token=)
end

Rails.application.initialize!

Rails.application.routes.draw do
  get "/", to: "home#index"
  get "/version", to: "home#version"
end

__LIB_TOUCH__

class HomeController < ActionController::Base
  def index
    render json: { message: "Hello World" }
  end

  def version
    render json: __VERSION_OBJ__
  end
end

run Rails.application
"""

# Hanami 0.x/1.x (buckets "0" and "1" share this template): hand-written
# single-file boot, confirmed directly from the hanami gem's OWN source
# doc-comments (lib/hanami/application.rb's class-level example +
# lib/hanami.rb's Hanami.configure/Hanami.app doc-comments) -- `Hanami.app`
# (which lazily calls `Hanami.boot`) is a real Rack app, the same
# integration point the comment says is used for `config.ru`
# (`run Hanami.app`). Bucket "0" uses the IDENTICAL Hanami::Application
# class (confirmed by diffing lib/hanami/application.rb between v0.9.2 and
# v1.3.5 -- the class-level doc-comment even says "@since 0.1.0"/
# "@since 0.2.0" for the exact same shape), so one template covers both --
# this is the genuinely different (pre-1->2-rewrite) API this registry's
# own notes call out, not a further split within 0.x/1.x.
_HANAMI1_CONFIG_RU = """\
require "hanami"
require "json"
require_relative "versions"
__LIB_IMPORTS__

__LIB_TOUCH__

module PqcScaApp
  class Application < Hanami::Application
    configure do
      routes do
        root to: ->(env) {
          [200, { "Content-Type" => "application/json" }, [{ message: "Hello World" }.to_json]]
        }

        get "/version", to: ->(env) {
          [200, { "Content-Type" => "application/json" }, [__VERSION_OBJ__.to_json]]
        }
      end
    end
  end
end

Hanami.configure do
  mount PqcScaApp::Application, at: "/"
end

run Hanami.app
"""

# Hanami 2.x/3.x: real slice-based `Hanami::App`/`Hanami::Routes` file
# layout, confirmed directly from hanami/hanami's own integration specs
# (spec/integration/setup_spec.rb + spec/integration/rack_app/
# rack_app_spec.rb, both fetched from the real v2.0.0-tagged source) --
# NOT guessed from general Rails-adjacent-framework assumptions. Hanami 3
# keeps the identical App/Routes file shape (confirmed via hanami's own
# 3.0.0 CHANGELOG.md, which documents purely additive changes -- mailer/
# i18n/logger integration -- on top of the same 2.x architecture, not a
# further rewrite; Hanami uses "Break Versioning" where any breaking
# change bumps the major, so 2->3 does not by itself imply a rewrite the
# way 1->2 genuinely was) -- one file layout covers both buckets ("2"/"3").
# `Hanami.boot` (required by config.ru) is itself just `require
# "hanami/setup"; Hanami.boot` (confirmed via lib/hanami/boot.rb source).
_HANAMI2_APP_RB = """\
require "hanami"
require_relative "../versions"
__LIB_IMPORTS__

module PqcScaApp
  class App < Hanami::App
  end
end

__LIB_TOUCH__
"""

_HANAMI2_ROUTES_RB = """\
require "json"

module PqcScaApp
  class Routes < Hanami::Routes
    root to: ->(env) {
      [200, { "Content-Type" => "application/json" }, [{ message: "Hello World" }.to_json]]
    }

    get "/version", to: ->(env) {
      [200, { "Content-Type" => "application/json" }, [__VERSION_OBJ__.to_json]]
    }
  end
end
"""

_HANAMI2_CONFIG_RU = """\
require "hanami/boot"

run Hanami.app
"""


# ── Gemfile generation ───────────────────────────────────────────────────────

def make_gemfile(fw_name: str, fw_major: str, fw_resolved: str, lib_name: str,
                 lib_resolved: str, needs_rackup: bool, lang_ver: str) -> str:
    lines = ['source "https://rubygems.org"', ""]
    fw_pkg = _FW_PACKAGE[fw_name]
    lines.append(f'gem "{fw_pkg}", "{fw_resolved}"')
    # Grape bucket 0 (resolves to its final patch, 0.19.2, released 2017)
    # leaves Rack completely unconstrained in its own gemspec ("rack, >=
    # 1.3.0") -- left to float, Bundler picks whatever's newest for the
    # installed Ruby (e.g. Rack 2.1.4.4 on Ruby 2.2), which breaks Grape's
    # OWN router at runtime: confirmed via a real crash (NoMethodError:
    # undefined method '[]' for nil:NilClass in Grape::Router#cascade?,
    # on every request). Pinning to Rack's own last 1.x release (the
    # actual era Grape 0.19.2 was built/tested against) fixes it --
    # confirmed via a real successful request on both Ruby 2.2 and 4.0.
    # Grape bucket 1 (resolves to its final patch, 1.8.0): its gemspec
    # dropped the earlier "< 3" ceiling (confirmed live: 'rack, >= 1.3.0',
    # no upper bound), but 1.8.0's OWN source (lib/grape.rb) still
    # `require`s 'rack/auth/digest/md5' -- a file Rack 3.0 deleted
    # outright. Confirmed via a real crash (LoadError: cannot load such
    # file -- rack/auth/digest/md5) once Rack 3.2.6 got resolved, and via
    # a real successful request once pinned to Rack's own last 2.x line.
    if fw_name == "Hanami" and fw_major in ("2", "3"):
        # Hanami 2.x's own gemspec does NOT depend on hanami-router at
        # all -- confirmed via its real gemspec (dry-*/hanami-cli/
        # hanami-utils/json/zeitwerk/rack-session, no hanami-router).
        # Routing is a genuinely optional, separately-installed
        # component in this architecture. Without it, Hanami::Slice#
        # load_routes's own `Hanami.bundled?("hanami-router")` guard
        # returns false before even attempting to require config/
        # routes.rb, so the app's Routes class silently never loads and
        # every single request 500s (NoMethodError: undefined method
        # 'call' for nil:NilClass in Hanami::Slice#rack_app) -- confirmed
        # via a real crash reproduced on every request, root-caused by
        # directly checking `gem "hanami-router"` inside the running
        # bundle (Gem::LoadError: hanami-router is not part of the
        # bundle), and confirmed fixed by adding it explicitly.
        lines.append('gem "hanami-router"')
    if fw_name == "Padrino" and _lang_ver_tuple(lang_ver) >= (4, 0):
        # padrino-core-0.16.1's own lib/padrino-core/configuration.rb
        # requires 'ostruct' unconditionally -- dropped from Ruby's own
        # default gems at 4.0 (same class of change as Grape-0's
        # ostruct/roqs's fiddle elsewhere in this module). Confirmed via
        # a real crash (LoadError: cannot load such file -- ostruct).
        # Only added on Ruby 4.0+, matching this module's own precedent:
        # already bundled by default on every older Ruby.
        lines.append('gem "ostruct"')
    if (fw_name, fw_major) == ("Grape", "1"):
        lines.append('gem "rack", "~> 2.2"')
    if (fw_name, fw_major) == ("Grape", "0"):
        lines.append('gem "rack", "~> 1.6"')
        # Grape 0.19.2 depends on virtus (parameter coercion, a feature
        # later dropped), which itself requires 'ostruct' -- a real
        # stdlib-turned-default-gem removal on Ruby 4.0+ (same class of
        # change as webrick's own 3.0 removal below). ONLY added on Ruby
        # 4.0+ (unlike webrick): the newest ostruct gem release (0.1.0)
        # itself uses the '&.' safe-navigation operator (Ruby 2.3+ only)
        # in its own source, confirmed via a real SyntaxError on Ruby
        # 2.2 -- adding it unconditionally would have swapped one crash
        # for another on every pre-4.0 Ruby, where it's already bundled
        # in stdlib anyway and doesn't need a separate gem at all.
        if _lang_ver_tuple(lang_ver) >= (4, 0):
            lines.append('gem "ostruct"')
    if lib_name == "roqs" and _lang_ver_tuple(lang_ver) >= (4, 0):
        # fiddle (roqs' own Fiddle-based struct definitions, see
        # _roqs_imports()) was dropped from Ruby's own default gems at
        # 4.0 -- confirmed via a real crash (LoadError: cannot load such
        # file -- fiddle). Only added here since it's already bundled by
        # default on every older Ruby.
        lines.append('gem "fiddle"')
    if lib_name == "argon2" or fw_name == "Rails":
        # ffi-compiler (argon2's own native-extension build dependency)
        # AND railties (Rails' own Rakefile-based task infrastructure)
        # INDEPENDENTLY depend on 'rake' completely unconstrained -- see
        # _rake_version()'s own docstring for the real failing build this
        # was confirmed against. Confirmed both paths are real: fixing
        # this only for argon2 still left plain Rails+jwt (no argon2 at
        # all) failing on the exact same rake-too-new error.
        lines.append(f'gem "rake", "{_rake_version(lang_ver)}"')
    if lib_name == "argon2":
        # argon2's own gemspec depends on 'ffi ~> 1.9' -- wide enough to
        # still float to whatever's newest overall, breaking on older
        # Ruby once THAT release's own floor outpaces it -- confirmed
        # via a real failing build (Ruby 2.1 + Rails 3 + argon2:
        # "ffi-1.17.4-x86_64-linux-musl requires ruby version < 4.1.dev,
        # >= 3.0, which is incompatible with the current version, ruby
        # 2.1.10").
        lines.append(f'gem "ffi", "{_era_gem_version("ffi", lang_ver, "1.12.2")}"')
    if fw_name == "Rails":
        # activesupport pulls in concurrent-ruby transitively with no
        # useful upper bound -- see _concurrent_ruby_version()'s own
        # docstring for the real failing build this was confirmed
        # against.
        lines.append(f'gem "concurrent-ruby", "{_concurrent_ruby_version(lang_ver)}"')
        # i18n pulls in multi_json transitively, also with no useful
        # upper bound -- confirmed via a real failing build right after
        # fixing concurrent-ruby (Ruby 2.1 + Rails 3 + argon2):
        # "multi_json-1.21.1 requires ruby version >= 3.2, which is
        # incompatible with the current version, ruby 2.1.10". 1.15.0's
        # own declared floor is unrestricted ('>= 0'), a safe fallback.
        lines.append(f'gem "multi_json", "{_era_gem_version("multi_json", lang_ver, "1.15.0")}"')
        # actionpack pulls in rack-cache transitively, also with no
        # useful upper bound -- confirmed via a real failing build right
        # after fixing multi_json (Ruby 2.1 + Rails 3 + argon2):
        # "rack-cache-1.17.0 requires ruby version >= 2.7.7, which is
        # incompatible with the current version, ruby 2.1.10".
        lines.append(f'gem "rack-cache", "{_era_gem_version("rack-cache", lang_ver, "1.2")}"')
        # railties pulls in thor (its CLI framework) transitively, also
        # with no useful upper bound -- confirmed via a real failing
        # build, plain Rails-3 + jwt (no argon2 at all): "thor-1.5.0
        # requires ruby version >= 2.6.0, which is incompatible with the
        # current version, ruby 2.1.10".
        lines.append(f'gem "thor", "{_era_gem_version("thor", lang_ver, "0.19.4")}"')
    if (fw_name, fw_major) == ("Rails", "3"):
        # activesupport-3.2.x's own lib/active_support/ruby/shim.rb
        # unconditionally `require`s 'active_support/core_ext/rexml',
        # which in turn requires 'rexml/rexml' -- confirmed via a real
        # crash on Ruby 3.1 (LoadError: cannot load such file --
        # rexml/rexml). rexml was removed from Ruby's own default gems
        # at 3.0 (demoted to a "bundled gem": still installed, but
        # invisible to Bundler.require unless declared in the Gemfile,
        # same class of change as webrick/ostruct/fiddle elsewhere in
        # this module). Confirmed this is a Rails-3-only issue: the
        # newer activesupport releases (4.2/5.2/6.1/7.2) only reference
        # rexml lazily inside xml_mini/rexml.rb, never at boot.
        # rexml's own oldest published release (3.1.7.3) has no Ruby
        # floor at all, so the era-resolved version is always safe here.
        lines.append(f'gem "rexml", "{_era_gem_version("rexml", lang_ver, "3.1.7.3")}"')
    # webrick was removed from Ruby's own stdlib bundling at 3.0 (still
    # perfectly installable as a normal gem on every tracked Ruby though)
    # -- added unconditionally (every framework here needs SOME Rack
    # handler: Sinatra's own Base#run! picks Puma/Falcon/WEBrick in that
    # order, everyone else boots via `rackup`'s bundled/separate
    # Rack::Handler::WEBrick). An UNCONSTRAINED `gem "webrick"` (no
    # version) still tells Bundler to resolve some real rubygems.org
    # release rather than just reusing Ruby's own stdlib-bundled copy on
    # Ruby <3.0 -- webrick's own later releases raised their floor too
    # (>=2.3.0, then >=2.4.0), so this hit the exact same class of issue
    # as every other unconstrained dependency in this module once enough
    # OTHER Gemfile pins changed how Bundler's resolver reached it --
    # confirmed via a real failing build (Ruby 2.1 + Rails 3 + jwt,
    # after fixing thor: "webrick-1.9.2 requires ruby version >= 2.4.0").
    lines.append(f'gem "webrick", "{_era_gem_version("webrick", lang_ver, "1.3.1")}"')
    if needs_rackup:
        lines.append('gem "rackup"')
    lines.append(f'gem "{_LIB_GEM[lib_name]}", "{lib_resolved}"')
    lines.append("")
    return "\n".join(lines)


# ── Dockerfile generation ────────────────────────────────────────────────────

# /usr/local/bundle/cache holds downloaded .gem files (BUNDLE_PATH's own
# cache) -- but bundler ALSO fetches rubygems.org's compact-index
# metadata (the "Fetching gem metadata from https://rubygems.org/..."
# step) into a completely separate location, /root/.bundle/cache,
# confirmed live via `Bundler.user_cache`. Without a cache mount there,
# every single build re-downloads the ENTIRE compact index from
# scratch -- confirmed as the real bottleneck in a live bulk test run
# (median 112s, worst case 2209s, JUST for that one step, and clearly
# worsening over the run's 3+ hours -- the same rate-limit-under-
# sustained-load pattern already hit and fixed for Java/Maven in this
# project). sharing=shared (not locked): purely cached HTTP metadata,
# safe for concurrent reads across parallel builds, and locking it
# would serialize builds against each other for no reason (same
# cache-lock-contention class of fix already applied elsewhere in this
# project's own generator safety fixes).
_BUNDLE_CACHE_MOUNT = (
    "--mount=type=cache,id=bundler-cache,target=/usr/local/bundle/cache,sharing=locked "
    "--mount=type=cache,id=bundler-metadata-cache,target=/root/.bundle/cache,sharing=shared"
)


def make_dockerfile(ruby_ver: str, fw_name: str, fw_major: str,
                    lib_name: str, lib_resolved: str, needs_rackup: bool) -> str:
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(ruby_ver)
    bundler_ver = (
        _bundler_version_1x(ruby_ver) if (fw_name, fw_major) == ("Rails", "3")
        else _bundler_version(ruby_ver)
    )

    # Cache-key diversifier (same reasoning/precedent as PHP's and Node's
    # own PQC_COMBO_ID/cache_bust ARGs): this Dockerfile template varies
    # by ruby_ver/lib_name but the actual Gemfile contents (fw+lib
    # versions) are what's COPYed in as a separate layer, so a stale
    # BuildKit cache hit on an unrelated combo sharing the same base image
    # + lib_name is a real, previously-confirmed-elsewhere risk class.
    cache_bust = f'ARG PQC_COMBO_ID="{fw_name}-{fw_major}-{lib_name}@{lib_resolved}"\n'

    liboqs_needed = lib_name in _NEEDS_LIBOQS
    libsodium_needed = lib_name in _NEEDS_LIBSODIUM

    # A C compiler is needed FAR more often than just bcrypt/argon2's own
    # native extension: every framework here (Grape via activesupport's
    # own bigdecimal/json/cgi deps; Rails directly the same way) pulls in
    # native-extension gems as part of its OWN transitive dependency
    # graph, completely independent of which crypto library is chosen --
    # confirmed via real failing builds ("You have to install development
    # tools first") on combos with NO compiler-needing library at all
    # (Rails 7 + openssl, Grape + openssl/jwt/digest/rbnacl/...). Always
    # installing build-essential in the (discarded) builder stage is a
    # trivial cost next to a broken build; the final image never carries
    # the compiler toolchain either way.
    #
    # git is only needed to clone liboqs -- bcrypt/argon2's own native
    # compile steps (mkmf/ffi-compiler) never shell out to git.
    builder_apt = ["build-essential"]
    if liboqs_needed:
        builder_apt += ["git", "cmake", "ninja-build", "build-essential", "pkg-config", "libssl-dev"]
    if lib_name == "openssl":
        # The standalone 'openssl' gem (unlike Ruby's own bundled openssl
        # stdlib copy) is a real native extension linking against system
        # libssl -- confirmed via a real failing build ("checking for
        # pkg-config for openssl... not found", "checking for
        # openssl/ssl.h... no"). Needs pkg-config + libssl-dev at INSTALL
        # time only, same as liboqs; never at runtime (every ruby:X-slim
        # base already ships libssl/libcrypto itself).
        builder_apt += ["pkg-config", "libssl-dev"]
    if fw_name == "Rails":
        # railties pulls in irb -> rdoc -> psych completely unconstrained
        # (same class of issue as rake/concurrent-ruby/multi_json/
        # rack-cache/thor/webrick elsewhere in this module, except this
        # one hits a missing SYSTEM header instead of a Ruby-floor
        # mismatch): the resolved psych release (5.4.0) has no
        # precompiled variant for this base and rebuilds its native
        # extension from source, which needs libyaml.h. Confirmed via a
        # real failing build ("checking for yaml.h... no", Gem::Ext::
        # BuildError installing psych) on Rails major 7 -- confirmed NOT
        # argon2-specific (identical failure on plain Rails-7 + jwt, no
        # argon2 at all).
        builder_apt += ["libyaml-dev"]
    # de-dupe while preserving order
    builder_apt = list(dict.fromkeys(builder_apt))

    liboqs_stage = _liboqs_build_stage() if liboqs_needed else ""

    # roqs looks for its native library at $ROQS_LIBOQS_DIR/<arch>/liboqs*
    # (or its own gem-internal native/<os>/<arch>/ dir) -- confirmed by
    # reading roqs' own lib/roqs/wrapper.rb source directly on GitHub, NOT
    # the standard ldconfig-registered /usr/local/lib search every other
    # liboqs consumer in this project relies on. Copying the built shared
    # library into a directory named after `uname -m` and pointing
    # ROQS_LIBOQS_DIR there is the real, minimum fix -- ldconfig alone
    # (sufficient for php-liboqs/jwt-pq/pqc_rails) is NOT enough for roqs.
    roqs_native_copy = (
        "RUN mkdir -p /opt/roqs-lib/$(uname -m) \\\n"
        "    && cp /usr/local/lib/liboqs.so* /opt/roqs-lib/$(uname -m)/\n"
        "ENV ROQS_LIBOQS_DIR=/opt/roqs-lib\n"
        if lib_name == "roqs" else ""
    )
    # jwt-pq's own extconf.rb downloads+compiles ITS OWN vendored liboqs
    # 0.16.0 tarball at `gem install` time by default (confirmed by
    # reading ext/jwt/pq/extconf.rb directly) -- JWT_PQ_USE_SYSTEM_LIBRARIES
    # skips that redundant build (writes a dummy Makefile instead) so it
    # reuses the SAME system liboqs.so this Dockerfile already builds for
    # roqs/pqc_rails; OQS_LIB pins the exact path its FFI loader
    # (lib/jwt/pq/liboqs.rb) checks first, ahead of any vendored/system
    # fallback search.
    jwt_pq_env = (
        "ENV JWT_PQ_USE_SYSTEM_LIBRARIES=1\n"
        "ENV OQS_LIB=/usr/local/lib/liboqs.so\n"
        if lib_name == "jwt-pq" else ""
    )
    # pqc_rails looks for liboqs at the OS-conventional default
    # (Linux: /usr/local/lib/liboqs.so, confirmed via its own README) --
    # exactly where this project's standard -DCMAKE_INSTALL_PREFIX=
    # /usr/local liboqs build already installs it, no extra copy/env
    # needed beyond ldconfig.

    # WEBrick (the Rack handler every combo here ends up using, added
    # unconditionally to every Gemfile) does a REVERSE DNS lookup of the
    # connecting client by default (webrick/server.rb's own
    # `sock.do_not_reverse_lookup = config[:DoNotReverseLookup]`, and
    # WEBrick's own default config value for that IS nil/false) -- for a
    # Docker-bridge client address (no PTR record) that lookup runs
    # against this container's real resolv.conf nameservers and stalls
    # for ~10s per connection before WEBrick will even start processing
    # the request. Confirmed via a real, exactly-reproducible ~10.0-10.1s
    # delay on every fresh connection (curl -v: TCP connects instantly,
    # then nothing for ~10s), and confirmed FIXED by passing
    # DoNotReverseLookup: true straight to the handler. `bundle exec
    # rackup` itself has no CLI flag for this WEBrick-specific option,
    # so config.ru-based combos boot via a small inline Rack::Builder +
    # Rack::Handler::WEBrick.run script instead. This is a real,
    # deterministic per-connection cost, not a flake -- it only showed up
    # as an intermittent "test failed, build succeeded" symptom because
    # this project's own test harness uses a 2s-per-attempt timeout,
    # which sometimes falls before vs after the container/network had
    # "warmed" a request naturally during build/port-detection overhead.
    # Rack 3.0 moved Rack::Handler (and Rack::Builder's own runtime, but
    # NOT the parse_file class method used below) into the separate
    # `rackup` gem, AS Rackup::Handler -- confirmed via a real crash
    # (NameError: uninitialized constant Rack::Handler) on every Rack-3
    # combo once the reverse-DNS fix above assumed the older
    # Rack::Handler::WEBrick location unconditionally. `require 'rackup'`
    # only succeeds (and only defines Rackup::Handler) on combos that
    # actually have that gem in their bundle (Rack 3-resolving ones);
    # falls back to the pre-3.0 Rack::Handler location otherwise --
    # confirmed both paths live (Rackup::Handler defined/Rack::Handler
    # nil on a Rack-3 combo, and the reverse on a Rack ~1.6/~2.2 one).
    _webrick_boot = (
        "require 'rack'; begin; require 'rackup'; rescue LoadError; end; "
        "app, _ = Rack::Builder.parse_file('config.ru'); "
        "h = defined?(Rackup::Handler) ? Rackup::Handler::WEBrick : Rack::Handler::WEBrick; "
        "h.run(app, Host: '0.0.0.0', Port: 8000, DoNotReverseLookup: true)"
    )
    if fw_name == "Sinatra":
        cmd = 'CMD ["ruby", "app.rb"]\n'
        app_copy = "COPY app.rb versions.rb ./\n"
    elif fw_name == "Hanami" and fw_major in ("2", "3"):
        cmd = f'CMD ["bundle", "_{bundler_ver}_", "exec", "ruby", "-e", "{_webrick_boot}"]\n'
        app_copy = "COPY config.ru versions.rb ./\nCOPY config ./config\n"
    else:
        # Pinning the exact same Bundler version used at build time
        # (not just bare `bundle exec`) matters whenever the framework
        # itself declares its own `bundler` version constraint (e.g.
        # Hanami 0.9.2's 'bundler, ~> 1.13') -- confirmed via a real
        # crash (Bundler::VersionConflict) when the container's own
        # default/system `bundle` (whatever ships with the base Ruby
        # image) didn't match Gemfile.lock's recorded BUNDLED WITH
        # version. Harmless to pin unconditionally: every other combo
        # already used this exact version to install in the first place.
        cmd = f'CMD ["bundle", "_{bundler_ver}_", "exec", "ruby", "-e", "{_webrick_boot}"]\n'
        app_copy = "COPY config.ru versions.rb ./\n"

    final_apt = []
    if libsodium_needed:
        final_apt.append(_libsodium_pkg(ruby_ver))

    bundler_install = (
        f"RUN gem install bundler -v \"{bundler_ver}\" --no-document\n"
    )
    bundle_install_cmd = (
        f"RUN {_BUNDLE_CACHE_MOUNT} \\\n"
        f"    bundle _{bundler_ver}_ install --jobs 4 --retry 3\n"
    )

    # argon2 0.x/1.x's own Makefile (ext/argon2_wrap) never copies its
    # compiled libargon2_wrap.so anywhere outside the ext/ build
    # directory (no real `make install` step -- confirmed via its
    # gem_make.out: 'make install' is just 'echo none'). Modern RubyGems
    # runs an automatic `make clean` right after building/"installing"
    # any C extension, which deletes that .so -- and since it was never
    # copied elsewhere, ffi-compiler's own runtime loader (which
    # recursively searches the gem's OWN directory tree for it) then
    # can't find it at all. Confirmed via a real crash (LoadError:
    # cannot find 'argon2_wrap' library) on Ruby 3.2+ specifically --
    # older RubyGems (Ruby 2.2's bundler 1.17.3) never ran that final
    # clean, so the .so happened to survive there by accident. argon2
    # 2.x's own Rakefile fixed this properly upstream (its gem_make.out
    # shows an explicit 'cp libargon2_wrap.so ../../lib' step that
    # survives the later clean) -- buckets 0/1 never got that fix, so
    # this recompiles+copies it ourselves the same way, only for those
    # two buckets (harmless no-op risk avoided by only running when
    # actually needed).
    argon2_relink = (
        f"RUN cd /usr/local/bundle/gems/argon2-{lib_resolved}/ext/argon2_wrap "
        "&& make && cp libargon2_wrap.so ../../lib/\n"
        if lib_name == "argon2" and lib_resolved.split(".")[0] in ("0", "1")
        else ""
    )

    # Always multi-stage: bcrypt/argon2/roqs/jwt-pq/pqc_rails need a real
    # compiler toolchain to produce their own compiled artifacts, but so
    # does virtually every OTHER combo now (see builder_apt's own comment
    # above) -- `builder` keeps cmake/ninja-build/build-essential/
    # pkg-config/libssl-dev (only the liboqs combos need the last three)
    # and runs `bundle install`; the final stage starts fresh from the
    # same `ruby:{ver}-slim` base and copies over just the installed gem
    # tree (/usr/local/bundle, which also holds any compiled native
    # extensions) plus liboqs's own compiled shared library where
    # relevant. No compiler toolchain at all ends up in the final image.
    # rbnacl's own FFI loader (RbNaCl::Sodium, "ffi_lib \"sodium\"") needs
    # an UNVERSIONED libsodium.so to resolve on old rbnacl/ffi gem pairs
    # -- the runtime libsodium*/libsodium-dev split only installs the
    # versioned SONAME symlink (libsodium.so.13/18/23), never the bare
    # libsodium.so one (that's libsodium-dev's job, which we don't want
    # just for this). Confirmed via a real crash on Ruby 2.2/jessie +
    # libsodium13 + rbnacl 3.4.0/ffi-1.12.2 ("Could not open library
    # 'sodium'... libsodium.so: cannot open shared object file") even
    # though `ldconfig -p` already resolves libsodium.so.13 correctly --
    # that old an ffi gem doesn't fall back to a versioned-SONAME search
    # the way newer ffi releases do. Creating the symlink ourselves,
    # unconditionally, sidesteps needing to know which ffi versions do
    # and don't have that fallback.
    libsodium_symlink = (
        "RUN ln -sf \"$(find /usr/lib -name 'libsodium.so.*' | head -1)\" "
        "/usr/lib/$(uname -m)-linux-gnu/libsodium.so\n"
        if libsodium_needed else ""
    )
    final_apt_block = (
        f"{apt_sources}"
        f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
        f"    {' '.join(final_apt)} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"{libsodium_symlink}"
        if final_apt else ""
    )
    return (
        "# syntax=docker/dockerfile:1\n"
        f"FROM ruby:{ruby_ver}-slim AS builder\n"
        f"{apt_sources}"
        f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
        f"    {' '.join(builder_apt)} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"{liboqs_stage}"
        f"{roqs_native_copy}"
        f"{jwt_pq_env}"
        f"{bundler_install}"
        "WORKDIR /app\n"
        f"{cache_bust}"
        "COPY Gemfile .\n"
        f"{bundle_install_cmd}"
        f"{argon2_relink}"
        f"{app_copy}"
        "\n"
        f"FROM ruby:{ruby_ver}-slim\n"
        f"{jwt_pq_env}"
        # No apt-get needed for libssl/libcrypto here -- confirmed by the
        # same reasoning already established for PHP's own equivalent
        # liboqs multi-stage final image in this project: every ruby:X-slim
        # base already ships libssl/libcrypto (Ruby's own bundled openssl
        # support links against it), and liboqs.so's own runtime needs
        # nothing beyond that. rbnacl's own runtime need (libsodium23) is
        # installed here via final_apt_block instead.
        + f"{final_apt_block}"
        + (f"COPY --from=builder /usr/local/lib/liboqs* /usr/local/lib/\nRUN ldconfig\n" if liboqs_needed else "")
        # roqs_native_copy must run AFTER liboqs.so has landed in
        # /usr/local/lib/ above (it copies FROM there), not before.
        + f"{roqs_native_copy}"
        + "WORKDIR /app\n"
        f"COPY --from=builder /usr/local/bundle /usr/local/bundle\n"
        f"COPY --from=builder /app/Gemfile ./\n"
        # Re-installing Bundler AFTER the /usr/local/bundle copy above
        # matters, not just doing it at all: this project's BUNDLE_PATH
        # is /usr/local/bundle, so a `gem install bundler` run BEFORE
        # that COPY gets its own bin/bundle shim silently overwritten by
        # the builder's copied-over one -- confirmed via a real crash
        # even with bundler 1.17.3 correctly gem-installed earlier in
        # the stage (`gem list` showed it present, `which bundle`
        # resolved to the copied-over /usr/local/bundle/bin/bundle
        # instead, whose own binstub doesn't recognize the `_X.Y.Z_`
        # version-selector syntax the same way). The base image only
        # ships whatever Bundler (if any) comes as a default gem for
        # that Ruby version -- never necessarily the SPECIFIC version
        # pinned above and used to install -- and the CMD below runs
        # `bundle _{bundler_ver}_ exec`, which needs that exact version
        # locally installed to resolve. Cheap: pure-Ruby gem, no
        # compilation.
        f"{bundler_install}"
        f"{app_copy}"
        "EXPOSE 8000\n"
        f"{cmd}"
    )


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write Gemfile / app files / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required gem
    version can't be resolved on rubygems.org, or when the framework/
    library pairing is a known structural impossibility (pqc_rails
    without a Rails 7/8 host).
    """
    out = images_base / "ruby" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    if lib_name == "pqc_rails" and _pqc_rails_needs_skip(fw_name, fw_major):
        print(f"  [SKIP] {fw_name} {fw_major} + pqc_rails: needs a real Rails "
              f">=7.1 host app (pqc_rails's own actionpack/activerecord/railties "
              f"deps are constrained '>=7.1,<9')", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    if lib_name == "argon2" and _argon2_rails_needs_skip(fw_name, fw_major, lang_ver):
        print(f"  [SKIP] {fw_name} {fw_major} + argon2 on Ruby {lang_ver}: argon2's "
              f"own ffi dependency resolves to a platform-specific precompiled "
              f"variant needing Ruby >=3.0, which no Gemfile pin can work around "
              f"(confirmed clean on Ruby 3.1+)", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    fw_pkg = _FW_PACKAGE[fw_name]
    try:
        fw_resolved = _resolve(fw_pkg, fw_major)
    except RubyGemsLookupError as exc:
        print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
        return False
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on rubygems.org", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    lib_pkg = _LIB_GEM[lib_name]
    try:
        lib_resolved = _resolve(lib_pkg, lib_ver)
    except RubyGemsLookupError as exc:
        print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
        return False
    if lib_resolved is None:
        print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on rubygems.org", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    out.mkdir(parents=True, exist_ok=True)

    meta = LIB_META[lib_name]
    imports = _roqs_imports() if lib_name == "roqs" else meta["imports"]
    touch = _argon2_touch(lib_ver) if lib_name == "argon2" else meta["touch"]
    version_obj = _sub(_VERSION_OBJ_RB, FW_NAME=fw_name, LIB_NAME=lib_name)
    needs_rackup = _needs_rackup(fw_name, fw_major, lang_ver)

    (out / "Gemfile").write_text(
        make_gemfile(fw_name, fw_major, fw_resolved, lib_name, lib_resolved, needs_rackup, lang_ver),
        encoding="utf-8",
    )
    (out / "versions.rb").write_text(_versions_rb(fw_resolved, lib_resolved), encoding="utf-8")

    if fw_name == "Sinatra":
        app_rb = _sub(_SINATRA_APP, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "app.rb").write_text(app_rb, encoding="utf-8")
    elif fw_name == "Grape":
        config_ru = _sub(_GRAPE_CONFIG_RU, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "config.ru").write_text(config_ru, encoding="utf-8")
    elif fw_name == "Roda":
        config_ru = _sub(_RODA_CONFIG_RU, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "config.ru").write_text(config_ru, encoding="utf-8")
    elif fw_name == "Padrino":
        config_ru = _sub(_PADRINO_CONFIG_RU, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "config.ru").write_text(config_ru, encoding="utf-8")
    elif fw_name == "Rails":
        config_ru = _sub(_RAILS_CONFIG_RU, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "config.ru").write_text(config_ru, encoding="utf-8")
    elif fw_name == "Hanami" and fw_major in ("0", "1"):
        config_ru = _sub(_HANAMI1_CONFIG_RU, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)
        (out / "config.ru").write_text(config_ru, encoding="utf-8")
    elif fw_name == "Hanami" and fw_major in ("2", "3"):
        (out / "config").mkdir(parents=True, exist_ok=True)
        app_rb = _sub(_HANAMI2_APP_RB, LIB_IMPORTS=imports, LIB_TOUCH=touch)
        routes_rb = _sub(_HANAMI2_ROUTES_RB, VERSION_OBJ=version_obj)
        (out / "config" / "app.rb").write_text(app_rb, encoding="utf-8")
        (out / "config" / "routes.rb").write_text(routes_rb, encoding="utf-8")
        (out / "config.ru").write_text(_HANAMI2_CONFIG_RU, encoding="utf-8")
    else:
        raise ValueError(f"Unknown framework: {fw_name} {fw_major}")

    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name, fw_major, lib_name, lib_resolved, needs_rackup),
        encoding="utf-8",
    )
    return True
