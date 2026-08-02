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


def _bundler_version(lang_ver: str) -> str:
    """Pick the newest Bundler release whose own declared ruby_version
    floor is satisfied by lang_ver, live-verified against rubygems.org's
    own per-version metadata (the same "ruby_version" field this module
    already reads for every other gem) rather than a hardcoded guess.

    Real, structural reason this matters: unlike Node/npm or newer Ruby,
    Bundler is NOT preinstalled at all in the old ruby:1.9/2.0/2.1-slim
    Docker images (Bundler only became a RubyGems "default gem" bundled
    with the interpreter itself starting around Ruby 2.6) -- every
    combo on an old Ruby bucket needs an explicit `gem install bundler`
    of a COMPATIBLE Bundler version before `bundle install` can run at
    all. Bundler 2.x itself keeps raising its own Ruby floor release over
    release (e.g. required >=2.3 for a while, then >=2.6), so even a
    "just pin some old Bundler 1.x" fallback would be wrong for the
    2.3-2.5 bucket range -- hence resolving this the same live-verified
    way as every other gem in this module, not guessing a cutoff table.
    """
    lv = _lang_ver_tuple(lang_ver)
    try:
        versions = _fetch_gem_versions("bundler")
    except RubyGemsLookupError:
        # Network problem -- fall back to the last Bundler 1.x release,
        # which is known to work all the way back to Ruby 1.8.7 and is
        # always a safe (if not maximally current) choice.
        return "1.17.3"

    floors = _GEM_RUBY_FLOOR.get("bundler", {})
    for v in sorted(versions, key=_ver_key, reverse=True):
        floor = _ruby_floor_tuple(floors.get(v))
        if lv >= floor:
            return v
    return "1.17.3"


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
#   - Grape 1.x explicitly excludes Rack 3 ("rack, >= 1.3.0, < 3"); Grape
#     2.x/3.x relax to "rack, >= 2" (no upper bound) and both only support
#     Ruby >=2.7/>=3.3 respectively (already >= Rack 3's own >=2.4.0 Ruby
#     floor) -- always resolves Rack 3.
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
                 ("Grape", "1"), ("Hanami", "0"), ("Hanami", "1"), ("Hanami", "2"),
                 ("Rails", "3"), ("Rails", "4"), ("Rails", "5"), ("Rails", "6")}
# Grape 0.x ("rack, >= 1.3.0", no upper bound) and Roda 3.x ("rack" with
# NO version constraint at all, confirmed via roda.gemspec) leave Rack
# fully up to whatever else is in the bundle -- since nothing else pins
# it for these two, Bundler resolves the newest Rack compatible with the
# INSTALLED RUBY ITSELF, so whether Rack 3 (Ruby floor >=2.4.0, confirmed
# via rack.gemspec at v3.0.0) is reachable depends on lang_ver, not on
# the framework major.
_RUBY_DEPENDENT_RACKUP = {("Grape", "0"), ("Roda", "3")}


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

# rbnacl is a pure-FFI binding (confirmed via its own gemspec: only
# depends on 'ffi', no C-extension compile of its own) needing the real
# libsodium shared library present as a SYSTEM (not build) dependency.
# bcrypt/argon2 are real native-extension gems needing a C compiler at
# INSTALL time only (bcrypt: ext/mri/bcrypt_ext.c via extconf.rb/mkmf;
# argon2: ffi-compiler compiling a vendored reference Argon2 C source) --
# not at runtime, so both benefit from a multi-stage split. roqs/jwt-pq/
# pqc_rails all need the real liboqs C library built from source (see
# _LIBOQS_TAG below) -- also a build-time-only cost once liboqs.so exists.
_NEEDS_LIBSODIUM = {"rbnacl"}
_NEEDS_COMPILER  = {"bcrypt", "argon2"}
_NEEDS_LIBOQS    = {"roqs", "jwt-pq", "pqc_rails"}
_MULTI_STAGE_LIBS = _NEEDS_COMPILER | _NEEDS_LIBOQS


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
# Per the registry's own '_comment_docker_repo' note: wheezy (1.9/2.0/2.1),
# jessie (2.2), stretch (2.3), buster (2.4/2.5) are the live-verified
# EOL Debian codenames needing the archive.debian.org redirect already
# established for Go/Node/PHP's own old bases in this project. 2.6+
# resolves to bullseye or later, still live on deb.debian.org.

_ARCHIVE_CODENAME_BY_VER = {
    "1.9": "wheezy",
    "2.0": "wheezy",
    "2.1": "wheezy",
    "2.2": "jessie",
    "2.3": "stretch",
    "2.4": "buster",
    "2.5": "buster",
}


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

def make_gemfile(fw_name: str, fw_resolved: str, lib_name: str, lib_resolved: str,
                 needs_rackup: bool) -> str:
    lines = ['source "https://rubygems.org"', ""]
    fw_pkg = _FW_PACKAGE[fw_name]
    lines.append(f'gem "{fw_pkg}", "{fw_resolved}"')
    # webrick was removed from Ruby's own stdlib bundling at 3.0 (still
    # perfectly installable as a normal gem on every tracked Ruby though)
    # -- added unconditionally (every framework here needs SOME Rack
    # handler: Sinatra's own Base#run! picks Puma/Falcon/WEBrick in that
    # order, everyone else boots via `rackup`'s bundled/separate
    # Rack::Handler::WEBrick) and harmless on Ruby <3.0 where it would
    # otherwise already be in stdlib.
    lines.append('gem "webrick"')
    if needs_rackup:
        lines.append('gem "rackup"')
    lines.append(f'gem "{_LIB_GEM[lib_name]}", "{lib_resolved}"')
    lines.append("")
    return "\n".join(lines)


# ── Dockerfile generation ────────────────────────────────────────────────────

_BUNDLE_CACHE_MOUNT = "--mount=type=cache,id=bundler-cache,target=/usr/local/bundle/cache,sharing=locked"


def make_dockerfile(ruby_ver: str, fw_name: str, fw_major: str,
                    lib_name: str, lib_resolved: str, needs_rackup: bool) -> str:
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(ruby_ver)
    bundler_ver = _bundler_version(ruby_ver)

    # Cache-key diversifier (same reasoning/precedent as PHP's and Node's
    # own PQC_COMBO_ID/cache_bust ARGs): this Dockerfile template varies
    # by ruby_ver/lib_name but the actual Gemfile contents (fw+lib
    # versions) are what's COPYed in as a separate layer, so a stale
    # BuildKit cache hit on an unrelated combo sharing the same base image
    # + lib_name is a real, previously-confirmed-elsewhere risk class.
    cache_bust = f'ARG PQC_COMBO_ID="{fw_name}-{fw_major}-{lib_name}@{lib_resolved}"\n'

    compiler_needed = lib_name in _NEEDS_COMPILER
    liboqs_needed = lib_name in _NEEDS_LIBOQS
    libsodium_needed = lib_name in _NEEDS_LIBSODIUM

    # git is only needed to clone liboqs -- bcrypt/argon2's own native
    # compile steps (mkmf/ffi-compiler) never shell out to git.
    builder_apt = []
    if compiler_needed:
        builder_apt.append("build-essential")
    if liboqs_needed:
        builder_apt += ["git", "cmake", "ninja-build", "build-essential", "pkg-config", "libssl-dev"]
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

    if fw_name == "Sinatra":
        cmd = 'CMD ["ruby", "app.rb"]\n'
        app_copy = "COPY app.rb versions.rb ./\n"
    elif fw_name == "Hanami" and fw_major in ("2", "3"):
        cmd = 'CMD ["bundle", "exec", "rackup", "-o", "0.0.0.0", "-p", "8000", "config.ru"]\n'
        app_copy = "COPY config.ru versions.rb ./\nCOPY config ./config\n"
    else:
        cmd = 'CMD ["bundle", "exec", "rackup", "-o", "0.0.0.0", "-p", "8000", "config.ru"]\n'
        app_copy = "COPY config.ru versions.rb ./\n"

    final_apt = []
    if libsodium_needed:
        final_apt.append("libsodium23")

    bundler_install = (
        f"RUN gem install bundler -v \"{bundler_ver}\" --no-document\n"
    )
    bundle_install_cmd = (
        f"RUN {_BUNDLE_CACHE_MOUNT} \\\n"
        f"    bundle _{bundler_ver}_ install --jobs 4 --retry 3\n"
    )

    if not _MULTI_STAGE_LIBS.intersection({lib_name}):
        # Single-stage: no compiler / liboqs build needed for this combo
        # (openssl/jwt/digest are pure-Ruby-or-stdlib; rbnacl is pure FFI
        # needing only the runtime libsodium .so, no compiler, no git) --
        # mirrors PHP's own "no compiler needed, multi-stage saves almost
        # nothing" reasoning for its equivalent plain-composer-install
        # combos. Only one apt-get layer, and only when there's actually
        # something to install (rbnacl's libsodium23; openssl/jwt/digest
        # need nothing beyond the base ruby:slim image).
        apt_block = (
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {' '.join(final_apt)} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            if final_apt else ""
        )
        return (
            "# syntax=docker/dockerfile:1\n"
            f"FROM ruby:{ruby_ver}-slim\n"
            f"{apt_block}"
            f"{bundler_install}"
            "WORKDIR /app\n"
            f"{cache_bust}"
            "COPY Gemfile .\n"
            f"{bundle_install_cmd}"
            f"{app_copy}"
            "EXPOSE 8000\n"
            f"{cmd}"
        )

    # Multi-stage: bcrypt/argon2 (native-extension compile) and roqs/
    # jwt-pq/pqc_rails (liboqs cmake/ninja build) all need a real compiler
    # toolchain ONLY to produce compiled artifacts (the *.so gem
    # extensions, or liboqs.so itself) -- never to run them. `builder`
    # keeps cmake/ninja-build/build-essential/pkg-config/libssl-dev (only
    # the liboqs combos need the last three) and runs `bundle install`;
    # the final stage starts fresh from the same `ruby:{ver}-slim` base
    # and copies over just the installed gem tree (/usr/local/bundle,
    # which also holds the compiled bcrypt_ext.so/argon2 native bits) plus
    # liboqs's own compiled shared library where relevant. No apt-get
    # compiler packages at all are needed in the final stage.
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
        f"{app_copy}"
        "\n"
        f"FROM ruby:{ruby_ver}-slim\n"
        f"{jwt_pq_env}"
        # No apt-get needed for libssl/libcrypto here -- confirmed by the
        # same reasoning already established for PHP's own equivalent
        # liboqs multi-stage final image in this project: every ruby:X-slim
        # base already ships libssl/libcrypto (Ruby's own bundled openssl
        # support links against it), and liboqs.so's own runtime needs
        # nothing beyond that.
        + (f"COPY --from=builder /usr/local/lib/liboqs* /usr/local/lib/\nRUN ldconfig\n" if liboqs_needed else "")
        # roqs_native_copy must run AFTER liboqs.so has landed in
        # /usr/local/lib/ above (it copies FROM there), not before.
        + f"{roqs_native_copy}"
        + "WORKDIR /app\n"
        f"COPY --from=builder /usr/local/bundle /usr/local/bundle\n"
        f"COPY --from=builder /app/Gemfile ./\n"
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
    imports = meta["imports"]
    touch = meta["touch"]
    version_obj = _sub(_VERSION_OBJ_RB, FW_NAME=fw_name, LIB_NAME=lib_name)
    needs_rackup = _needs_rackup(fw_name, fw_major, lang_ver)

    (out / "Gemfile").write_text(
        make_gemfile(fw_name, fw_resolved, lib_name, lib_resolved, needs_rackup),
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
