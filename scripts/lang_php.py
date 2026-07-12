"""
PHP-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_php").

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

LANGUAGE_ID   = "php"
REGISTRY_FILE = "registry php.json"


# ── Packagist version resolution ────────────────────────────────────────────────
# Packagist's 'p2' metadata API (repo.packagist.org/p2/{vendor}/{package}.json)
# is the direct analog of Maven's maven-metadata.xml / NuGet's flatcontainer
# index: it lists every published version. Like Maven/NuGet and unlike npm,
# Composer's `require` pins an EXACT version per install (no range
# resolution at install time once composer.lock exists) -- so the same
# "bake the resolved version into a generated file, don't bother reading it
# back at runtime" reasoning Java/.NET rely on applies here too (see
# _versions_php() below): Composer 1.x (needed for PHP <7.2) has no runtime
# equivalent of Composer\InstalledVersions (a Composer 2.0+-only feature),
# so baking at generation time is also the only approach that works
# uniformly across this project's whole PHP version range, not just the
# simplest one.

_PACKAGIST_VERSIONS: dict = {}

# Composer/SemVer prereleases use a literal hyphen (-alpha, -beta, -RC1,
# -dev), the same convention as NuGet -- a "no hyphen" regex is sufficient.
_STABLE_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    except ValueError:
        return (0,)


def _fetch_packagist_versions(package: str) -> list:
    if package in _PACKAGIST_VERSIONS:
        return _PACKAGIST_VERSIONS[package]

    url = f"https://repo.packagist.org/p2/{package}.json"
    versions = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        raw = data.get("packages", {}).get(package, [])
        # Composer versions are inconsistently 'v'-prefixed depending on the
        # package's own tagging convention (Laravel/Symfony use 'v10.0.0',
        # Slim/phpseclib/sodium_compat usually don't) -- strip it uniformly.
        candidates = (v.get("version", "").lstrip("vV") for v in raw)
        versions = sorted((v for v in candidates if _STABLE_RE.match(v)), key=_ver_key)
    except (URLError, OSError, ValueError) as exc:
        print(f"  [WARN] Packagist lookup failed for {package}: {exc}", flush=True)

    _PACKAGIST_VERSIONS[package] = versions
    return versions


def _resolve(package: str, registry_ver: str) -> str | None:
    """Resolve a registry version like '10' or '2.5' to the latest matching
    stable release on Packagist (e.g. '10' -> '10.50.2')."""
    versions = _fetch_packagist_versions(package)

    prefix = registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in versions:
        return registry_ver

    return None


# ── Framework metadata ────────────────────────────────────────────────────────

_FW_PACKAGE: dict = {
    "Laravel": "laravel/framework",
    "Symfony": "symfony/symfony",
    "Slim":    "slim/slim",
}

# Slim's routing API has FOUR incompatible shapes across its tracked majors
# (verified against real code -- an actual `docker run` of a built Slim 1
# image threw "Class 'Slim\Slim' not found", which is what caught this;
# inspecting vendor/slim/slim/Slim/Slim.php directly showed `class Slim {`
# with NO namespace declaration at all -- Slim 1.x predates the project's
# adoption of PHP 5.3 namespaces entirely, unlike 2.x which already uses
# `Slim\Slim`): 1.x uses the bare global `new \Slim()` class with a closure
# taking no request/response params; 2.x is the same closure shape but
# namespaced as `new \Slim\Slim()`; 3.x uses `new \Slim\App()` with
# PSR-7-ish request/response objects passed into the closure,
# `$response->write(...)` (mutates in place, no re-assignment)
# `->withHeader(...)` (returns a new instance); 4.x uses the newer
# `AppFactory::create()` entry point with true PSR-7 `$response->getBody()->
# write(...)` + `->withHeader(...)`.
_SLIM_MAJOR_ERA = {
    "1": "v1",
    "2": "v2",
    "3": "v3",
    "4": "v4",
}


# ── Crypto library metadata ────────────────────────────────────────────────────
# "touch" mirrors Java/.NET's LIB_META convention: a real call into the
# library so it's provably loaded and exercised, not just declared as a
# dependency. phpseclib's touch code is version-dependent (its namespace
# changed at every major -- see _phpseclib_touch()) so it isn't a plain
# string the way the others are.

LIB_META: dict = {
    "openssl": {
        "imports": "",
        "touch": "openssl_digest('touch', 'sha256');",
    },
    "sodium": {
        "imports": "",
        "touch": "sodium_crypto_generichash('touch');",
    },
    "sodium_compat": {
        "imports": "",
        "touch": "\\ParagonIE_Sodium_Compat::crypto_generichash('touch');",
    },
    "phpseclib": {
        "imports": "",
        # Placeholder -- real per-major touch code is substituted in
        # write_context() via _phpseclib_touch(lib_ver), since phpseclib's
        # namespace differs by major (see below).
        "touch": "",
    },
    "php-liboqs": {
        "imports": "",
        # Defensive, matches Java Tink's / .NET's PQC try/catch-wrapped
        # touch: php-liboqs is young, PQC availability itself is not in
        # question (liboqs is built from source in the same Dockerfile
        # stage, see make_dockerfile()) but a defensive try/catch still
        # protects against any as-yet-unknown runtime edge case in this
        # specific young extension, consistent with how this project
        # treats every other early-stage PQC library.
        "touch": (
            "try {\n"
            "    \\OQS\\KEM::keypair(\\OQS\\KEM::ALG_ML_KEM_768);\n"
            "} catch (\\Throwable $e) { /* exercised, ignore init failure */ }"
        ),
    },
}

_BUILTIN_LIBS = frozenset({"openssl", "sodium"})


def _phpseclib_touch(bucket: str) -> str:
    """phpseclib's Random API changed shape at every major -- confirmed by
    inspecting each branch's actual vendor source after 1.x's assumed
    'Crypt_Random::string()' threw a real "Class not found" in an actual
    docker run: 1.x's Crypt/Random.php declares NO class at all, just a
    bare global function crypt_random_string($length); 2.x introduced the
    \\phpseclib\\ namespace with a real Random::string() static method;
    3.x renamed the namespace to \\phpseclib3\\ but kept the same method."""
    if bucket == "1":
        return "\\crypt_random_string(16);"
    if bucket == "2":
        return "\\phpseclib\\Crypt\\Random::string(16);"
    return "\\phpseclib3\\Crypt\\Random::string(16);"


def _lib_package(lib_name: str) -> str | None:
    if lib_name in _BUILTIN_LIBS:
        return None
    return {
        "sodium_compat": "paragonie/sodium_compat",
        "phpseclib":     "phpseclib/phpseclib",
        "php-liboqs":    "secudoc/php-liboqs",
    }[lib_name]


# ── Pre-fetch ─────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from Packagist for every package this run needs."""
    packages: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        pkg = _FW_PACKAGE.get(fw["name"])
        if pkg:
            packages.add(pkg)
    for lib in lang_data.get("cryptography_libs", []):
        if lib.get("version") == "built-in":
            continue
        pkg = _lib_package(lib["name"])
        if pkg:
            packages.add(pkg)

    print("Fetching available versions from Packagist ...")
    for pkg in sorted(packages):
        versions = _fetch_packagist_versions(pkg)
        print(f"  {pkg}: {len(versions)} version(s) found")
    print()


# ── Docker image / Composer / Debian-archive helpers ───────────────────────────
# Verified live against each php:{version}-cli image's own /etc/os-release:
# 5.6/7.0 -> stretch, 7.1/7.2 -> buster (both dropped from the live Debian
# mirrors -- confirmed 404 for buster directly), 7.3/8.0 -> bullseye
# (CONFIRMED STILL LIVE via a real apt-get update, Debian's LTS program
# keeps it on deb.debian.org past its mainstream EOL), 8.1+ -> trixie
# (current stable, live). Only stretch/buster need the archive.debian.org
# redirect this project already established for Go/Node's own old bases.

_ARCHIVE_CODENAME_BY_VER = {
    "5.6": "stretch",
    "7.0": "stretch",
    "7.1": "buster",
    "7.2": "buster",
}


def _debian_archive_apt(php_ver: str) -> tuple:
    codename = _ARCHIVE_CODENAME_BY_VER.get(php_ver)
    apt_sources = (
        f"RUN echo 'deb http://archive.debian.org/debian {codename} main' > /etc/apt/sources.list \\\n"
        f"    && echo 'deb http://archive.debian.org/debian-security {codename}/updates main' >> /etc/apt/sources.list\n"
        if codename else ""
    )
    apt_flag     = "-o Acquire::Check-Valid-Until=false " if codename else ""
    allow_unauth = "--allow-unauthenticated "              if codename else ""
    return apt_sources, apt_flag, allow_unauth


# Composer 2.3+ requires PHP >=7.2.5 -- our buckets are major.minor only,
# but every included version at or above "7.2" resolves to a patch well
# past .5, so the boundary is safe at the bucket level. Below that, the
# 'composer:2.2' LTS tag is used instead of 'composer:1': Packagist has
# fully sunset its Composer-1-compatible metadata protocol (confirmed via
# an actual docker build -- `composer:1`'s `composer install` fails with
# "package could not be found in any version" for EVERY package, not a
# PHP-version-specific failure), so composer:1 can no longer install
# anything regardless of target PHP version. Composer 2.2 is upstream's
# own officially-maintained LTS branch specifically for pre-7.2.5 PHP
# (composer:2.3+ itself prints this exact suggestion when run under old
# PHP) and still speaks the modern Packagist API. Confirmed working via a
# real `docker build` copying composer:2.2's binary into a php:5.6-cli
# image and running `composer --version` successfully.
def _composer_tag(php_ver: str) -> str:
    major, minor = (int(x) for x in php_ver.split("."))
    return "2.2" if (major, minor) < (7, 2) else "2"


# php-liboqs's oqs.c calls OQS_KEM_encaps_derand(), which does NOT exist in
# liboqs 0.14.0 (confirmed by grepping the actual tagged src/kem/kem.h --
# absent at 0.14.0, present starting 0.15.0) despite php-liboqs's own README
# claiming "liboqs 0.14.0 or newer" as its floor. Found via a real
# `docker build` compile failure (implicit-declaration warning-as-error on
# that exact symbol) before switching to 0.15.0, the latest actual stable
# tag (0.16.0 only exists as an -rc1 prerelease as of this writing).
_LINUX_LIBOQS_TAG = "0.15.0"


# ── App templates ─────────────────────────────────────────────────────────────
# json_encode()/json_decode() have been part of PHP core since 5.2 -- unlike
# Java/.NET, no hand-rolled JSON helper is needed anywhere in this module.

_VERSION_OBJ_PHP = """[
        'language' => ['name' => 'PHP', 'version' => phpversion()],
        'framework' => ['name' => '__FW_NAME__', 'version' => FRAMEWORK_VERSION],
        'library' => ['name' => '__LIB_NAME__', 'version' => LIB_VERSION],
    ]"""

# Laravel/Symfony are used via their decoupled HTTP-message classes directly
# (Illuminate\Http\JsonResponse / Symfony\Component\HttpFoundation\
# JsonResponse) with manual URI branching, rather than through either
# framework's full routing/DI-container bootstrap -- both frameworks are
# normally scaffolded via a SEPARATE skeleton package (laravel/laravel,
# symfony/skeleton) installed via `composer create-project`, not by
# `require`-ing the framework metapackage directly into a bare script the
# way this project's minimal 2-endpoint apps need. This mirrors the
# project's other "full framework needs real scaffolding, so exercise its
# real HTTP classes via manual routing instead" precedents (.NET's legacy
# ASP.NET Core middleware, NancyFx's Owin-bridge hosting choice) rather than
# inventing a bespoke skeleton just for this generator.
_LARAVEL_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

use Illuminate\\Http\\JsonResponse;

__LIB_TOUCH__

$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);

if ($path === '/version') {
    $response = new JsonResponse(__VERSION_OBJ__);
} else {
    $response = new JsonResponse(['message' => 'Hello World']);
}

$response->send();
"""

_SYMFONY_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

use Symfony\\Component\\HttpFoundation\\JsonResponse;

__LIB_TOUCH__

$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);

if ($path === '/version') {
    $response = new JsonResponse(__VERSION_OBJ__);
} else {
    $response = new JsonResponse(['message' => 'Hello World']);
}

$response->send();
"""

# Slim 4 -- true PSR-7, the shape this project's template was originally
# researched against.
_SLIM_V4_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

use Slim\\Factory\\AppFactory;
use Psr\\Http\\Message\\ResponseInterface as Response;
use Psr\\Http\\Message\\ServerRequestInterface as Request;

__LIB_TOUCH__

$app = AppFactory::create();

$app->get('/', function (Request $request, Response $response) {
    $response->getBody()->write(json_encode(['message' => 'Hello World']));
    return $response->withHeader('Content-Type', 'application/json');
});

$app->get('/version', function (Request $request, Response $response) {
    $response->getBody()->write(json_encode(__VERSION_OBJ__));
    return $response->withHeader('Content-Type', 'application/json');
});

$app->run();
"""

# Slim 3 -- `new \\Slim\\App()`, PSR-7-ish response object with the same
# write()/withHeader() shape as 4.x but a different app-construction entry
# point (AppFactory didn't exist yet).
_SLIM_V3_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

__LIB_TOUCH__

$app = new \\Slim\\App();

$app->get('/', function ($request, $response) {
    $response->getBody()->write(json_encode(['message' => 'Hello World']));
    return $response->withHeader('Content-Type', 'application/json');
});

$app->get('/version', function ($request, $response) {
    $response->getBody()->write(json_encode(__VERSION_OBJ__));
    return $response->withHeader('Content-Type', 'application/json');
});

$app->run();
"""

# Slim 1.x -- the bare global `Slim` class, no namespace at all. Confirmed
# via an actual `docker run` of a built Slim 1 image (which threw "Class
# 'Slim\\Slim' not found" against the namespaced template below) followed by
# inspecting vendor/slim/slim/Slim/Slim.php directly: `class Slim {` with no
# `namespace` declaration -- Slim 1.x predates this project's other
# namespaced-era assumption entirely.
_SLIM_V1_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

__LIB_TOUCH__

$app = new \\Slim();

$app->get('/', function () use ($app) {
    $app->contentType('application/json');
    echo json_encode(['message' => 'Hello World']);
});

$app->get('/version', function () use ($app) {
    $app->contentType('application/json');
    echo json_encode(__VERSION_OBJ__);
});

$app->run();
"""

# Slim 2.x -- namespaced `\\Slim\\Slim`, same no-request/response-param
# closure shape and response()->headers->set(...) accessor as 1.x.
_SLIM_V2_INDEX = """\
<?php
require __DIR__ . '/vendor/autoload.php';
require __DIR__ . '/versions.php';
__LIB_IMPORTS__

__LIB_TOUCH__

$app = new \\Slim\\Slim();

$app->get('/', function () use ($app) {
    $app->response()->headers->set('Content-Type', 'application/json');
    echo json_encode(['message' => 'Hello World']);
});

$app->get('/version', function () use ($app) {
    $app->response()->headers->set('Content-Type', 'application/json');
    echo json_encode(__VERSION_OBJ__);
});

$app->run();
"""

_SLIM_INDEX_BY_ERA = {
    "v1": _SLIM_V1_INDEX,
    "v2": _SLIM_V2_INDEX,
    "v3":     _SLIM_V3_INDEX,
    "v4":     _SLIM_V4_INDEX,
}


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


def _versions_php(fw_resolved: str, lib_resolved: str) -> str:
    return (
        "<?php\n"
        f"define('FRAMEWORK_VERSION', '{fw_resolved}');\n"
        f"define('LIB_VERSION', '{lib_resolved}');\n"
    )


# ── composer.json generation ────────────────────────────────────────────────────
# Slim 4 externalized its PSR-7 implementation into a separate optional
# package (any of slim/psr7, nyholm/psr7, guzzlehttp/psr7 -- a full skeleton
# picks one via `composer create-project`); bare `slim/slim` alone throws at
# runtime ("Could not detect any PSR-17 ResponseFactory implementation").
# Found via an actual `docker run` + curl of a built Slim 4 image, not
# assumed from the docs -- Slim 3.x doesn't need this, its own bundled
# Slim\Http\Response already implements PSR-7 in-package.
_SLIM_PSR7_PACKAGE = "slim/psr7"


def make_composer_json(fw_name: str, fw_major: str, fw_resolved: str,
                       lib_name: str, lib_resolved: str) -> str:
    require: dict = {}
    fw_pkg = _FW_PACKAGE.get(fw_name)
    if fw_pkg:
        require[fw_pkg] = fw_resolved
    if fw_name == "Slim" and _SLIM_MAJOR_ERA.get(fw_major) == "v4":
        require[_SLIM_PSR7_PACKAGE] = "*"
    # php-liboqs is Packagist's `type: php-ext` marker package (it
    # `"replace"`s the virtual ext-oqs, it has no PHP source to autoload)
    # -- a plain `composer install` can't build it (confirmed via an actual
    # docker build: "these were not loaded, likely because it conflicts
    # with another require"). The real .so is already compiled directly
    # into PHP via phpize/make install in the Dockerfile; only its version
    # number (resolved from Packagist) is used for /version reporting.
    lib_pkg = _lib_package(lib_name)
    if lib_pkg and lib_name != "php-liboqs":
        require[lib_pkg] = lib_resolved

    doc = {
        "name": "pqc/app",
        "type": "project",
        "require": require,
        "minimum-stability": "stable",
        # Composer 2.2+ blocks any third-party Composer PLUGIN (not just
        # advisory-flagged packages) unless explicitly allow-listed --
        # found via an actual docker build failure on Laravel 4 (a
        # transitive dependency, kylekatarnls/update-helper, ships a
        # plugin and got blocked by default). Blanket-allowed here for the
        # same reason security-blocking is disabled above: this project
        # deliberately builds old dependency trees on purpose, in a
        # throwaway build context, so the plugin-trust tradeoff Composer's
        # default is protecting against doesn't apply.
        "config": {"allow-plugins": True},
    }
    return json.dumps(doc, indent=4) + "\n"


# ── Dockerfile generation ─────────────────────────────────────────────────────
# The official 'composer' Docker image's binary is copied out via a
# multi-stage COPY (the standard, Composer-docs-recommended pattern) rather
# than curl-installed inside the app image -- the phar is a self-contained
# script with no OS dependency on the container it was copied FROM, only on
# the php executable it's later run WITH. Composer 1 vs 2 (see
# _composer_tag()) is the one place PHP's own version genuinely matters for
# this, since Composer 2.x's own phar requires PHP >=7.2.5 to even parse.
#
# No PHP `zip` extension is compiled in (would need libzip-dev + a
# docker-php-ext-install step) -- Composer falls back to the `unzip` CLI
# tool for package extraction when the extension isn't present, which is
# lighter-weight and avoids a compile step entirely.

def make_dockerfile(php_ver: str, lib_name: str) -> str:
    composer_tag = _composer_tag(php_ver)
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(php_ver)

    liboqs_stage = ""
    liboqs_copy = ""
    extra_apt = "unzip git"
    if lib_name == "php-liboqs":
        extra_apt = "unzip git cmake ninja-build gcc g++ pkg-config libssl-dev"
        liboqs_stage = (
            f"RUN git clone --depth 1 --branch {_LINUX_LIBOQS_TAG} \\\n"
            "    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \\\n"
            "    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \\\n"
            "       -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \\\n"
            "       -DOQS_BUILD_ONLY_LIB=ON -GNinja \\\n"
            "    && cmake --build /tmp/liboqs/build --target install \\\n"
            "    && rm -rf /tmp/liboqs && ldconfig\n"
        )
        liboqs_copy = (
            "RUN git clone --depth 1 https://github.com/secudoc/php-liboqs /tmp/php-liboqs \\\n"
            "    && cd /tmp/php-liboqs \\\n"
            "    && phpize \\\n"
            "    && ./configure --with-php-config=$(command -v php-config) --with-oqs=/usr/local \\\n"
            "    && make -j$(nproc) \\\n"
            "    && make install \\\n"
            "    && docker-php-ext-enable oqs \\\n"
            "    && rm -rf /tmp/php-liboqs\n"
        )

    # Composer 2.4+ blocks installing any package version with a known
    # Packagist security advisory by default (e.g. phpseclib 1.0.30) --
    # confirmed via an actual docker build failure ("these were not loaded,
    # because they are affected by security advisories"). This project
    # deliberately builds old/vulnerable library versions on purpose (that's
    # the whole point of the fingerprinting/migration-path research), so the
    # block is disabled with --no-security-blocking. The 2.2 LTS branch used
    # for old PHP predates this flag entirely (confirmed: passing it there
    # is a hard "option does not exist" error), so it's added only for the
    # 'composer:2' tag.
    security_flag = " --no-security-blocking" if composer_tag == "2" else ""

    return (
        f"FROM composer:{composer_tag} AS composer\n"
        "\n"
        f"FROM php:{php_ver}-cli\n"
        f"{apt_sources}"
        f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
        f"    {extra_apt} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"{liboqs_stage}"
        f"{liboqs_copy}"
        "COPY --from=composer /usr/bin/composer /usr/local/bin/composer\n"
        "WORKDIR /app\n"
        "ENV COMPOSER_ALLOW_SUPERUSER=1\n"
        "COPY composer.json .\n"
        f"RUN composer install --no-dev --no-interaction --prefer-dist{security_flag}\n"
        "COPY index.php versions.php ./\n"
        "EXPOSE 8000\n"
        'CMD ["php", "-d", "display_errors=0", "-d", "log_errors=1", '
        '"-S", "0.0.0.0:8000", "index.php"]\n'
    )


# Laravel 4's own composer.json hard-requires `phpseclib/phpseclib: 0.3.*`
# on EVERY v4.x tag (confirmed via the real Packagist p2 metadata for
# laravel/framework -- every v4.1/4.2 release listed requires exactly
# "0.3.*", never anything else across the whole v4 line). None of this
# registry's three tracked phpseclib buckets resolve to a 0.3.x release
# (bucket "1" is the legacy-but-still-1.x branch, latest patch 1.0.30) so
# Composer always hits "requires phpseclib 0.3.* ... but it conflicts with
# your root composer.json require" for every bucket, not just one --
# confirmed via a real `composer install` failure. This is a genuine
# same-package-two-versions impossibility (Composer can't install two
# versions of phpseclib/phpseclib in one tree), not a convenience
# exclusion, so the whole Laravel-4 x phpseclib combo is skipped.
_INCOMPATIBLE_COMBOS = {("Laravel", "4", "phpseclib")}


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write composer.json / index.php / versions.php / Dockerfile for one
    image context.

    Returns False (and removes any stale directory) when a required
    Packagist package version cannot be resolved, or when the
    framework/library pairing is a known unresolvable dependency conflict.
    """
    out = images_base / "php" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    if (fw_name, fw_major, lib_name) in _INCOMPATIBLE_COMBOS:
        print(f"  [SKIP] {fw_name} {fw_major} + {lib_name}: "
              f"hard version conflict in {fw_name}'s own composer.json", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    fw_pkg = _FW_PACKAGE[fw_name]
    fw_resolved = _resolve(fw_pkg, fw_major)
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on Packagist", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    if lib_name in _BUILTIN_LIBS:
        lib_resolved = "built-in"
    else:
        lib_pkg = _lib_package(lib_name)
        lib_resolved = _resolve(lib_pkg, lib_ver)
        if lib_resolved is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on Packagist", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    out.mkdir(parents=True, exist_ok=True)

    meta = LIB_META[lib_name]
    imports = meta["imports"]
    touch = _phpseclib_touch(lib_ver) if lib_name == "phpseclib" else meta["touch"]
    version_obj = _sub(_VERSION_OBJ_PHP, FW_NAME=fw_name, LIB_NAME=lib_name)

    if fw_name == "Laravel":
        tpl = _LARAVEL_INDEX
    elif fw_name == "Symfony":
        tpl = _SYMFONY_INDEX
    elif fw_name == "Slim":
        tpl = _SLIM_INDEX_BY_ERA[_SLIM_MAJOR_ERA.get(fw_major, "v4")]
    else:
        raise ValueError(f"Unknown framework: {fw_name}")

    index_php = _sub(tpl, LIB_IMPORTS=imports, LIB_TOUCH=touch, VERSION_OBJ=version_obj)

    (out / "index.php").write_text(index_php, encoding="utf-8")
    (out / "versions.php").write_text(_versions_php(fw_resolved, lib_resolved), encoding="utf-8")
    (out / "composer.json").write_text(
        make_composer_json(fw_name, fw_major, fw_resolved, lib_name, lib_resolved), encoding="utf-8"
    )
    (out / "Dockerfile").write_text(make_dockerfile(lang_ver, lib_name), encoding="utf-8")
    return True
