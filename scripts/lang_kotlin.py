"""
Kotlin-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_kotlin").

Required exports:
    LANGUAGE_ID   - str
    REGISTRY_FILE - str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool

Language-version axis is the Kotlin COMPILER version (see registry kotlin.json's
own "_comment_version_axis"), not JDK -- JDK/Gradle-builder-image selection is a
pure implementation detail resolved here (_TOOLCHAIN), never a registry field.
Every framework/library tracked in this registry lives on Maven Central (same
as Java), so Maven-metadata resolution is reused directly from lang_java.py
rather than re-implemented -- check_updates.py's "maven" fetch_kind already
hardcodes an import of lang_java for exactly this reason, so this module's own
version lookups stay byte-for-byte consistent with what that scanner checks.
"""

import re
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError

import lang_java

LANGUAGE_ID   = "kotlin"
REGISTRY_FILE = "registry kotlin.json"

MavenLookupError = lang_java.MavenLookupError


# ── Builder-image / JDK selection (implementation detail, NOT a registry axis) ─
# kotlinc's own JDK floor never rose past 8 (confirmed live through 2.2), so the
# real constraint driving which JDK/Gradle a given Kotlin line needs is the
# Kotlin Gradle Plugin's own minimum-Gradle requirement crossed with which JDKs
# that Gradle version can itself run under (docs.gradle.org/current/userguide/
# compatibility.html) -- Gradle 9.x notably requires JDK 17-26 just to EXECUTE,
# regardless of what kotlinc itself needs, so the newest Kotlin lines are gated
# upward by build tooling, not by the language. One (jdk, gradle-image-tag) pair
# per Kotlin line, chosen as the oldest Gradle/JDK combo confirmed (live, via
# Docker Hub's tag-existence API) to satisfy that line's own KGP-to-Gradle floor:
#   1.0-1.2 (2016-2017 era, pre-dates any documented KGP/Gradle matrix): JDK 8,
#            Gradle 4.10.3 -- last Gradle 4.x, safely older than every
#            constraint this project could find for these three lines.
#   1.3-1.5 (2018-2021): JDK 8, Gradle 5.6.4 -- Gradle 5.x floor, still JDK-8-run.
#   1.6-1.7: JDK 11, Gradle 6.9.4 -- KGP needs Gradle >=6.1.1 (1.6) / >=6.7.1
#            (1.7); last Gradle 6.x release satisfies both with headroom.
#   1.8-2.0: JDK 17, Gradle 7.6.4 -- KGP needs Gradle >=6.8.3 for all three;
#            Gradle 7.6 also lets the runtime/builder JDK move to 17.
#   2.1-2.2: JDK 21, Gradle 8.10.2 -- KGP needs Gradle >=7.6.3.
#   2.3-2.4: JDK 21, Gradle 9.6.0 -- the newest KGP patches need Gradle up to
#            9.6.0; Gradle 9.x's own JDK-17-to-26-to-run floor is why this tier
#            can no longer offer JDK 8/11 the way older Kotlin lines could, even
#            though kotlinc itself would happily run on either.
# Every exact "{gradle}-jdk{n}" tag was confirmed live against Docker Hub's tag
# API before being hardcoded here (see this project's own repeated "matching
# base image when debugging" lesson from the Rust rollout).
_TOOLCHAIN: dict = {
    "1.0": ("8",  "4.10.3-jdk8"),
    "1.1": ("8",  "4.10.3-jdk8"),
    "1.2": ("8",  "4.10.3-jdk8"),
    "1.3": ("8",  "5.6.4-jdk8"),
    "1.4": ("8",  "5.6.4-jdk8"),
    # 1.5 was originally grouped with 1.3/1.4 (Gradle 5.6.4), but the Kotlin
    # Gradle Plugin itself refuses to even load under Gradle 5.6.4 starting
    # at 1.5.x -- confirmed via a real build failure ("The current Gradle
    # version 5.6.4 is not compatible with the Kotlin Gradle plugin. Please
    # use Gradle 6.1.1 or newer") and independently confirmed that the SAME
    # Gradle 5.6.4 loads KGP 1.4.32 just fine, isolating the boundary to
    # exactly this line. Reuses the already-proven 1.6/1.7 tier rather than
    # introducing a new one -- kotlinc itself never needed more than JDK 8,
    # so bumping the JDK here too is harmless, just unused headroom.
    "1.5": ("11", "6.9.4-jdk11"),
    "1.6": ("11", "6.9.4-jdk11"),
    "1.7": ("11", "6.9.4-jdk11"),
    "1.8": ("17", "7.6.4-jdk17"),
    "1.9": ("17", "7.6.4-jdk17"),
    "2.0": ("17", "7.6.4-jdk17"),
    "2.1": ("21", "8.10.2-jdk21"),
    "2.2": ("21", "8.10.2-jdk21"),
    "2.3": ("21", "9.6.0-jdk21"),
    "2.4": ("21", "9.6.0-jdk21"),
}


# Quarkus's own official Gradle plugin (latest 3.x patch, 3.38.1) requires
# Gradle >=8.14 -- confirmed via a real build failure ("This version of
# Quarkus is tested with Gradle 8.14 or later", a genuine NoClassDefFoundError
# on org.gradle.api.artifacts.ResolvableConfiguration under this project's
# older default Gradle tiers). Bumping the Gradle version alone does NOT fix
# this for every Kotlin line, though: Kotlin 2.0.21's own Kotlin Gradle
# Plugin has an independent compatibility CEILING with newer Gradle --
# confirmed via two separate real failures, one under Gradle 8.14.2 and
# again under Gradle 9.6.0, both the identical error ('void org.jetbrains.
# kotlin.incremental.IncrementalCompilationFeatures.<init>...'), a binary
# API mismatch between that KGP release and modern Gradle's own bundled
# Kotlin-DSL-support classes. Since Quarkus's own floor keeps rising and
# older Kotlin lines' KGP releases were never built against anything that
# new, this project's fix is to NARROW which Kotlin lines are tracked as
# Quarkus-compatible at all (see registry kotlin.json's Quarkus notes) to
# just the lines whose OWN default toolchain tier already sits at Gradle
# 9.6.0 (2.3/2.4) -- no per-framework Gradle-version override needed at all
# once the registry itself only ever asks for those two lines.
def _toolchain(kotlin_ver: str, fw_name: str = "") -> tuple:
    return _TOOLCHAIN[kotlin_ver]


# Two separate Gradle/Kotlin-tooling era boundaries, confirmed via real build
# failures ("Unresolved reference: mainClass" / "Unresolved reference:
# jvmToolchain") rather than assumed -- see _gradle_build_kts()'s own
# comments for the full story. These do NOT share the same cutoff: Gradle's
# own application.mainClass Property landed at Gradle 6.4 (this project's
# 1.6 tier already uses 6.9.4, past that), while the Kotlin Gradle Plugin's
# own jvmToolchain() DSL function didn't exist until KGP 1.7.0 (one tier
# later).
_OLD_MAINCLASS_LINES = frozenset({"1.0", "1.1", "1.2", "1.3", "1.4", "1.5"})
_OLD_JVM_TARGET_LINES = frozenset({"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6"})


# ── Framework metadata ─────────────────────────────────────────────────────────
# "anchor" is the (groupId, artifactId) whose own maven-metadata.xml version
# list is queried to resolve a registry major like "2" to "2.3.13". Ktor's
# anchor artifact itself changed at the KMP split (see registry notes) -- major
# 1 must be looked up via the un-suffixed artifact (the only one that existed
# then), majors 2/3 via the "-jvm" leaf (the un-suffixed one becomes a Gradle-
# Module-Metadata variant root from 2.0.0-beta-1 on, with no per-version list
# of its own reachable via a plain maven-metadata.xml GET).
FRAMEWORK_META: dict = {
    "Ktor":        {"anchor_v1": ("io.ktor", "ktor-server-core"),
                     "anchor":    ("io.ktor", "ktor-server-core-jvm")},
    "Spring Boot": {"anchor": ("org.springframework.boot", "spring-boot-starter-parent")},
    "http4k":      {"anchor": ("org.http4k", "http4k-core")},
    "Micronaut":   {"anchor": ("io.micronaut", "micronaut-core")},
    "Quarkus":     {"anchor": ("io.quarkus", "quarkus-bom")},
}


def _framework_anchor(fw_name: str, fw_major: str) -> tuple:
    if fw_name == "Ktor" and fw_major == "1":
        return FRAMEWORK_META["Ktor"]["anchor_v1"]
    if fw_name == "Micronaut":
        # micronaut-core's own version numbers do NOT match the parent/BOM
        # coordinate's -- confirmed by a real Gradle failure ("Could not
        # find io.micronaut.platform:micronaut-parent:4.10.26", a version
        # that was never published under that groupId; micronaut-core's
        # own latest at the time was 4.10.26, but micronaut-parent's real
        # latest 4.x was a different patch entirely). lang_java.py's own
        # generator resolves Micronaut's framework version against the
        # parent/BOM coordinate directly for exactly this reason (see its
        # own _framework_anchor()) -- registry's "module" field still shows
        # micronaut-core for check_updates.py's tracking purposes, same as
        # Java's own registry, but actual version RESOLUTION must use the
        # real artifact this project's build.gradle.kts will reference.
        return _micronaut_bom_coord(fw_major)
    return FRAMEWORK_META[fw_name]["anchor"]


# Micronaut's parent-POM coordinate isn't lockstep with micronaut-core's own
# version numbers, and the groupId/mechanism changed across majors -- reuses
# the exact same per-major split lang_java.py already established
# (_MICRONAUT_PARENT_BY_MAJOR/_MICRONAUT_PARENT_DEFAULT), consumed here via
# Gradle's platform() dependency notation instead of a Maven <parent>.
_MICRONAUT_BOM_BY_MAJOR: dict = {
    "1": ("io.micronaut", "micronaut-bom"),
    "2": ("io.micronaut", "micronaut-parent"),
    "3": ("io.micronaut", "micronaut-parent"),
}
_MICRONAUT_BOM_DEFAULT = ("io.micronaut.platform", "micronaut-parent")  # 4.x/5.x


def _micronaut_bom_coord(fw_major: str) -> tuple:
    return _MICRONAUT_BOM_BY_MAJOR.get(fw_major, _MICRONAUT_BOM_DEFAULT)


_MICRONAUT_NO_SERDE_MAJORS = frozenset({"1", "2"})


# Same per-major REST-layer split lang_java.py's own Quarkus entry documents:
# javax.ws.rs/quarkus-resteasy(-jackson) for majors 1/2, jakarta.ws.rs/
# quarkus-rest-jackson from 3.x onward -- identical facts, just consumed from
# Kotlin instead of Java.
_QUARKUS_JAXRS_PKG_BY_MAJOR = {"1": "javax.ws.rs", "2": "javax.ws.rs"}
_QUARKUS_JAXRS_PKG_DEFAULT = "jakarta.ws.rs"  # 3.x onward

_QUARKUS_REST_DEPS_BY_MAJOR = {
    "1": [("io.quarkus", "quarkus-resteasy"), ("io.quarkus", "quarkus-resteasy-jackson")],
    "2": [("io.quarkus", "quarkus-resteasy"), ("io.quarkus", "quarkus-resteasy-jackson")],
}
_QUARKUS_REST_DEPS_DEFAULT = [("io.quarkus", "quarkus-rest-jackson")]  # 3.x onward


def _quarkus_jaxrs_pkg(fw_major: str) -> str:
    return _QUARKUS_JAXRS_PKG_BY_MAJOR.get(fw_major, _QUARKUS_JAXRS_PKG_DEFAULT)


def _quarkus_rest_deps(fw_major: str) -> list:
    return _QUARKUS_REST_DEPS_BY_MAJOR.get(fw_major, _QUARKUS_REST_DEPS_DEFAULT)


# The actual build.gradle.kts dependency coordinate stays un-suffixed
# ("io.ktor:ktor-server-netty"/"ktor-server-core") for EVERY major -- Gradle's
# own GMM-aware resolver picks the right platform variant automatically for a
# JVM project, unlike a raw maven-metadata.xml fetch which needs the explicit
# "-jvm" leaf from 2.0.0-beta-1 onward. Only the version-lookup anchor above
# needs the per-major split; the dependency declaration never does.
_KTOR_ENGINE_ARTIFACT = "ktor-server-netty"
_KTOR_CORE_ARTIFACT   = "ktor-server-core"


# ── Library metadata ───────────────────────────────────────────────────────────
# coord=None means built into the JDK (JCA); "source-build" libs (liboqs-kotlin)
# carry coord=None too but are special-cased in write_context() -- they have no
# Maven Central artifact to resolve at all (confirmed live: a 404 on Maven
# Central for org/openquantumsafe/liboqs-java), so their "version" is the exact
# git tag, never a resolver output.
LIB_META: dict = {
    "JCA": {
        "coord": None,
        "imports": ["java.security.Security"],
        "touch": "Security.getProviders()",
    },
    "BouncyCastle": {
        "coord": ("org.bouncycastle", "bcprov-jdk18on"),
        "imports": ["java.security.Security", "org.bouncycastle.jce.provider.BouncyCastleProvider"],
        "touch": "Security.addProvider(BouncyCastleProvider())",
    },
    "Tink": {
        "coord": ("com.google.crypto.tink", "tink"),
        "imports": ["com.google.crypto.tink.aead.AeadConfig"],
        "touch": "try { AeadConfig.register() } catch (e: Exception) { /* exercised, ignore init failure */ }",
    },
    "Conscrypt": {
        "coord": ("org.conscrypt", "conscrypt-openjdk-uber"),
        "imports": ["java.security.Security", "org.conscrypt.Conscrypt"],
        "touch": "Security.addProvider(Conscrypt.newProvider())",
    },
    "KotlinCrypto": {
        "coord": ("org.kotlincrypto.hash", "sha2-jvm"),
        "imports": ["org.kotlincrypto.hash.sha2.SHA256"],
        "touch": 'SHA256().digest("probe".toByteArray())',
    },
    "liboqs-kotlin": {
        "coord": None,  # source-build, see registry notes
        "imports": ["org.openquantumsafe.KeyEncapsulation"],
        # dispose_KEM(), not "cleanup()" -- confirmed via liboqs-java's own
        # examples/KEMExample.java after a real compile failure ("Unresolved
        # reference 'cleanup'"); that's the real, correct teardown method.
        "touch": 'KeyEncapsulation("ML-KEM-768").let { it.generate_keypair(); it.dispose_KEM() }',
    },
}

# Same legacy-artifact-rename precedent as lang_java.py's _BC_LEGACY_BUCKET --
# the "1.70" bucket is the last release under the pre-rename bcprov-jdk15on
# coordinate; every other bucket uses bcprov-jdk18on.
_BC_LEGACY_BUCKET = "1.70"
_BC_LEGACY_COORD  = ("org.bouncycastle", "bcprov-jdk15on")

# liboqs (the C library) tag built in the liboqs-kotlin builder stage -- reuses
# the exact tag this project's PHP rollout already pinned (php-liboqs's own
# README-claimed 0.14.0 floor was confirmed wrong; 0.15.0 is the real floor for
# OQS_KEM_encaps_derand). Same underlying C library regardless of language
# binding, so there is no reason to pick a different tag here.
_LIBOQS_C_TAG = "0.15.0"
# liboqs-java's own latest tagged release (see registry kotlin.json's
# liboqs-kotlin notes -- only 0.3.0 is tracked in this first pass). Confirmed
# live via the GitHub API that this repo's own tags carry NO "v" prefix
# (unlike many other OQS-org repos) -- a real docker build caught this: an
# initial "v0.3.0" guess failed with "Remote branch v0.3.0 not found".
_LIBOQS_JAVA_TAG = "0.3.0"

_JACKSON_KOTLIN_COORD = ("com.fasterxml.jackson.module", "jackson-module-kotlin")


def _lib_coord(lib_name: str, lib_ver_bucket: str | None = None):
    if lib_name == "BouncyCastle" and lib_ver_bucket == _BC_LEGACY_BUCKET:
        return _BC_LEGACY_COORD
    return LIB_META[lib_name]["coord"]


# ── Maven Central version resolution (delegated to lang_java, see module note) ─

def _resolve(group: str, artifact: str, registry_ver: str) -> str | None:
    return lang_java._resolve(group, artifact, registry_ver)


def _ver_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


# Ktor's own pinned kotlin_version/libs.versions.toml "kotlin" property
# drifts at the PATCH level, not just per-minor -- confirmed live (real
# per-tag gradle.properties/libs.versions.toml fetches) after a real docker
# build failure: resolving Ktor major "3" to its own absolute latest patch
# (3.5.2, built with Kotlin 2.3.21) against an OLDER target compiler like
# Kotlin 2.1.21 fails outright ("Module was compiled with an incompatible
# version of Kotlin. The binary version of its metadata is 2.3.0, expected
# version is 2.1.0") -- Kotlin binary metadata is forward-readable (an
# older jar built with an older Kotlin works fine under a newer compiler)
# but NOT backward-readable. An initial fix used a per-MINOR floor table
# (one representative tag checked per X.Y line), which caught the major-1
# case too (1.0.x/1.1.x/1.2.x/1.3.x pin Kotlin 1.3.x, 1.4.x/1.5.x pin
# 1.4.x, 1.6.x's FIRST patch pins 1.5.10) -- but a SECOND real build
# failure (Kotlin 1.5 + Ktor 1) proved even that was insufficient: Ktor
# 1.6.8 (the latest patch within the SAME 1.6.x minor) actually pins
# Kotlin 1.6.10, confirmed via a full per-patch sweep of 1.6.0 through
# 1.6.8 showing the pin climbing THREE more times within that one minor
# (1.6.0/1.6.1→1.5.10, 1.6.2/1.6.3→1.5.20, 1.6.4→1.5.30, 1.6.5→1.5.31,
# 1.6.6/1.6.7→1.6.0, 1.6.8→1.6.10, the last one under yet another config
# key, 'kotlin-version' with a hyphen, not 'kotlin_version'/'kotlin') --
# and the same intra-minor drift was independently confirmed in 1.3.x and
# 1.4.x too. A per-minor table is fundamentally the wrong granularity here.
# _resolve_ktor() below instead does a live, per-PATCH walk from the
# newest version downward until it finds one whose OWN actual pin (fetched
# fresh, not read from any static table) satisfies the target line --
# genuinely robust to this drift instead of assuming any single sampled
# patch represents its whole minor.
_KTOR_PIN_CACHE: dict = {}


def _ktor_patch_kotlin_pin(tag: str) -> str | None:
    """Live-fetches the exact Kotlin version one specific Ktor tag pins,
    trying every config file/key this project has confirmed Ktor using
    across its history (old gradle.properties, and libs.versions.toml
    under both the 'kotlin' and 'kotlin-version' keys different eras use).
    Cached per tag since the same patch can be re-checked across multiple
    (major, kotlin_line) resolution calls."""
    if tag in _KTOR_PIN_CACHE:
        return _KTOR_PIN_CACHE[tag]
    result = None
    file_cache: dict = {}
    for path, pattern in (
        ("gradle.properties", r"(?m)^kotlin_version\s*=\s*([\d.]+)"),
        ("gradle/libs.versions.toml", r'(?m)^kotlin\s*=\s*"([\d.]+)"'),
        ("gradle/libs.versions.toml", r'(?m)^kotlin-version\s*=\s*"([\d.]+)"'),
    ):
        if path not in file_cache:
            try:
                req = urllib.request.Request(
                    f"https://raw.githubusercontent.com/ktorio/ktor/{tag}/{path}",
                    headers={"User-Agent": "curl/8.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    file_cache[path] = resp.read().decode("utf-8", errors="ignore")
            except (URLError, OSError):
                file_cache[path] = ""
        m = re.search(pattern, file_cache[path])
        if m:
            result = m.group(1)
            break
    _KTOR_PIN_CACHE[tag] = result
    return result


_KTOR_RESOLVE_CACHE: dict = {}


def _resolve_ktor(fw_major: str, kotlin_line: str) -> str | None:
    """Walks Ktor's real patches for the target major, newest first, live-
    checking each one's own actual Kotlin pin (never a static per-minor
    table -- see the module comment above for why that was insufficient),
    returning the first (i.e. newest) patch whose pin doesn't exceed the
    target line. Applies uniformly across every Ktor major (1/2/3); major
    1 just uses a different Maven coordinate for the version-list fetch
    (the pre-KMP-split 'ktor-server-core', not '-jvm'). Cached per (major,
    kotlin_line) pair since generate_images.py calls this once per library/
    version combination sharing the same framework major and target line."""
    cache_key = (fw_major, kotlin_line)
    if cache_key in _KTOR_RESOLVE_CACHE:
        return _KTOR_RESOLVE_CACHE[cache_key]

    group, artifact = (
        FRAMEWORK_META["Ktor"]["anchor_v1"] if fw_major == "1" else FRAMEWORK_META["Ktor"]["anchor"]
    )
    versions = lang_java._fetch_maven_versions(group, artifact)
    patches = sorted(
        (v for v in versions if v.startswith(fw_major + ".")),
        key=_ver_key, reverse=True,
    )
    target = _ver_key(kotlin_line)
    result = None
    for patch in patches:
        pin = _ktor_patch_kotlin_pin(patch)
        if pin is None:
            continue  # couldn't determine this patch's pin -- try the next-older one
        if _ver_key(".".join(pin.split(".")[:2])) <= target:
            result = patch
            break
    _KTOR_RESOLVE_CACHE[cache_key] = result
    return result


# http4k releases essentially continuously (100+ real minors per major,
# confirmed via Maven Central) and its own required Kotlin compiler version
# drifts CONTINUOUSLY within a single major, not just at major boundaries the
# way Ktor's ~10 total minors did -- a full per-minor floor table the way
# Ktor's _KTOR_MINOR_KOTLIN_FLOOR works would need 100+ hand-verified entries
# per major to be safe. Instead, each major here is represented by a small
# set of hand-picked, individually-verified EXACT checkpoint versions (their
# own real Kotlin pin fetched live from that exact tag's versions.properties/
# libs.versions.toml, not interpolated) -- resolution picks the highest
# checkpoint whose own confirmed floor doesn't exceed the target Kotlin line.
# See registry kotlin.json's http4k notes for the full sweep data behind
# these specific checkpoints.
_HTTP4K_CHECKPOINTS: dict = {
    "5": {"1.8": "5.0.0.0", "1.9": "5.20.0.0", "2.0": "5.47.0.0"},
    "6": {"2.0": "6.0.1.0", "2.1": "6.15.1.0", "2.2": "6.20.2.0", "2.3": "6.50.0.0", "2.4": "6.57.1.0"},
}


def _resolve_http4k(fw_major: str, kotlin_line: str) -> str | None:
    checkpoints = _HTTP4K_CHECKPOINTS.get(fw_major, {})
    target = _ver_key(kotlin_line)
    eligible = [(floor, ver) for floor, ver in checkpoints.items() if _ver_key(floor) <= target]
    if not eligible:
        return None
    # Highest-floor checkpoint still <= target -- the newest, best match.
    return max(eligible, key=lambda pair: _ver_key(pair[0]))[1]


# Micronaut's own Kotlin annotation-processing mechanism genuinely changed
# across majors -- confirmed via two real, compounding build/runtime bugs:
# (1) io.micronaut:micronaut-inject-kotlin as its own separately-published
# artifact only EXISTS from Micronaut 4.0.0 onward (confirmed via its own
# maven-metadata.xml, whose oldest entry is 4.0.0-M1) -- majors 1-3 have no
# such artifact at all. (2) The 4.x/5.x micronaut-inject-kotlin jar registers
# ONLY a KSP SymbolProcessorProvider service, no javax.annotation.processing.
# Processor at all (confirmed by inspecting its own META-INF/services/) --
# kapt genuinely cannot invoke it (a real docker run confirmed this: the
# build succeeded, kaptKotlin ran with no errors, but zero routes were ever
# registered, because kapt only understands the javax.annotation.processing.
# Processor SPI and this jar doesn't implement it). Majors 1-3 instead use
# kapt against io.micronaut:micronaut-inject-java (confirmed via jar
# inspection at both a major-1-era and major-3-era version: this artifact
# DOES register javax.annotation.processing.Processor) -- this is the
# original, pre-KSP mechanism: kapt generates Java stubs from Kotlin source,
# and micronaut-inject-java processes those stubs exactly as if they were
# real Java sources.
_MICRONAUT_KSP_MAJORS = frozenset({"4", "5"})

_KSP_COORD = ("com.google.devtools.ksp", "symbol-processing-gradle-plugin")
_KSP_RAW_VERSIONS: list | None = None
_KSP_VERSIONS: dict = {}


def _fetch_ksp_raw_versions() -> list:
    """KSP's own versioning scheme ('{kotlin patch}-{ksp release}', e.g.
    '2.0.21-1.0.28') doesn't match lang_java._fetch_maven_versions()'s
    stable-release regex (which requires a plain dotted-number string,
    rejecting anything with a hyphen) -- that function would silently filter
    out every real KSP version, so this fetches the raw maven-metadata.xml
    directly instead, applying only a light RC/Beta prerelease exclusion."""
    global _KSP_RAW_VERSIONS
    if _KSP_RAW_VERSIONS is not None:
        return _KSP_RAW_VERSIONS
    group, artifact = _KSP_COORD
    group_path = group.replace(".", "/")
    safe_artifact = urllib.parse.quote(artifact, safe="")
    url = f"https://repo1.maven.org/maven2/{group_path}/{safe_artifact}/maven-metadata.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        raw = [v.text for v in root.findall(".//versions/version") if v.text]
        versions = [v for v in raw if re.match(r"^[\d.]+-[\d.]+$", v)]
    except (URLError, ET.ParseError, OSError) as exc:
        raise MavenLookupError(f"{group}:{artifact}: {exc}") from exc
    _KSP_RAW_VERSIONS = versions
    return versions


def _ksp_version(kotlin_resolved: str) -> str | None:
    """KSP releases are versioned '{exact kotlin patch}-{ksp release}' (e.g.
    '2.0.21-1.0.28') -- an exact-patch match is preferred, since KSP is tied
    to the Kotlin compiler's own internal APIs at that patch, but KSP
    releases can lag a freshly-cut Kotlin patch by days/weeks, so this falls
    back to the latest KSP release for any patch sharing the same
    major.minor line (still real, still compatible in practice -- Kotlin's
    own compiler-plugin ABI is stable within a minor line) rather than
    failing the whole combo outright."""
    if kotlin_resolved in _KSP_VERSIONS:
        return _KSP_VERSIONS[kotlin_resolved]
    versions = _fetch_ksp_raw_versions()
    exact = sorted((v for v in versions if v.startswith(kotlin_resolved + "-")), key=_ver_key)
    if exact:
        result = exact[-1]
    else:
        minor_prefix = ".".join(kotlin_resolved.split(".")[:2]) + "."
        same_minor = sorted((v for v in versions if v.startswith(minor_prefix)), key=_ver_key)
        result = same_minor[-1] if same_minor else None
    _KSP_VERSIONS[kotlin_resolved] = result
    return result


_JACKSON_KOTLIN_LATEST: dict = {}


def _jackson_kotlin_version() -> str:
    """jackson-module-kotlin isn't a per-major registry axis (Spring Boot's own
    Kotlin support doesn't pin a specific companion version the way it pins
    kotlinVersion) -- any reasonably current release is compatible with the
    Jackson Boot itself pulls in transitively, so this just resolves to
    whatever is newest on Maven Central, fetched once and cached."""
    if "v" in _JACKSON_KOTLIN_LATEST:
        return _JACKSON_KOTLIN_LATEST["v"]
    group, artifact = _JACKSON_KOTLIN_COORD
    versions = lang_java._fetch_maven_versions(group, artifact)
    latest = versions[-1] if versions else "2.18.2"
    _JACKSON_KOTLIN_LATEST["v"] = latest
    return latest


# ── Pre-fetch ───────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from Maven Central for all coordinates,
    including the Kotlin compiler's own kotlin-stdlib (used to resolve e.g.
    '2.4' -> '2.4.10')."""
    coords: set = {("org.jetbrains.kotlin", "kotlin-stdlib")}
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        for fv in fw.get("version", []):
            coords.add(_framework_anchor(fw["name"], fv["nr"]))
        if fw["name"] == "Ktor":
            coords.add(("io.ktor", _KTOR_ENGINE_ARTIFACT))
            coords.add(("io.ktor", _KTOR_CORE_ARTIFACT))
        if fw["name"] == "Spring Boot":
            coords.add(_JACKSON_KOTLIN_COORD)
        if fw["name"] == "http4k":
            coords.add(("org.http4k", "http4k-server-undertow"))
        if fw["name"] == "Micronaut":
            for fv in fw.get("version", []):
                coords.add(_micronaut_bom_coord(fv["nr"]))
        if fw["name"] == "Quarkus":
            coords.add(("io.quarkus", "quarkus-kotlin"))
            coords.add(("io.quarkus", "quarkus-arc"))
            for fv in fw.get("version", []):
                coords.update(_quarkus_rest_deps(fv["nr"]))
    for lib in lang_data.get("cryptography_libs", []):
        if lib.get("version") == "built-in" or lib["name"] == "liboqs-kotlin":
            continue
        for lv in lib.get("version", []):
            coord = _lib_coord(lib["name"], lv["nr"])
            if coord:
                coords.add(coord)

    print("Fetching available versions from Maven Central ...")
    for group, artifact in sorted(coords):
        try:
            versions = lang_java._fetch_maven_versions(group, artifact)
            print(f"  {group}:{artifact}: {len(versions)} version(s) found")
        except MavenLookupError as exc:
            print(f"  [WARN] {exc}")
    print()


# ── versions.properties (runtime version read) ──────────────────────────────────
# Same sidestep as lang_java.py's own versions.properties: Gradle, like Maven,
# pins an EXACT resolved version per dependency, so whatever this generator
# resolves is exactly what gets installed -- baked directly into a plain
# resource file at generation time, read back with one uniform helper.

def _versions_properties(fw_resolved: str, lib_resolved: str) -> str:
    return (
        f"framework.version={fw_resolved}\n"
        f"library.version={lib_resolved}\n"
    )


# Spring Boot's embedded Tomcat defaults to port 8080, NOT this project's
# uniform 8000 -- unlike Ktor (where the port is an explicit embeddedServer()
# argument in Main.kt itself), Spring Boot needs it set via config. Confirmed
# missing by a real docker run: the app started and answered correctly on
# 8080 (checked via `docker exec ... curl localhost:8080/`), but the
# container's published 8000->host mapping got nothing at all. Same
# application.properties mechanism lang_java.py's own _PORT_CONFIG already
# uses for its Spring Boot entry.
_PORT_CONFIG = {
    "Spring Boot": "server.port=8000\n",
    "Ktor": None,
    "http4k": None,  # port is an explicit Undertow(8000) argument in Main.kt itself
    "Micronaut": "micronaut.server.port=8000\n",
    # quarkus.package.jar.type is a build-time property Quarkus's own
    # augmentation step reads straight from application.properties (there is
    # no separate Gradle-DSL equivalent the way a Maven <properties> entry
    # is) -- without it Quarkus defaults to its fast-jar layout (a
    # quarkus-app/ directory + quarkus-run.jar + lib/), not the single
    # app-runner.jar this project's Dockerfile CMD expects. Matches
    # lang_java.py's own uber-jar packaging choice for consistency.
    "Quarkus": "quarkus.http.port=8000\nquarkus.package.jar.type=uber-jar\n",
}


_VERSIONS_READ_HELPER = """\
private object __Res__

private fun versions(): java.util.Properties {
    val p = java.util.Properties()
    __Res__::class.java.getResourceAsStream("/versions.properties")?.use { p.load(it) }
    return p
}

private fun esc(s: String): String =
    "\\"" + s.replace("\\\\", "\\\\\\\\").replace("\\"", "\\\\\\"") + "\\""
"""


# ── App templates ───────────────────────────────────────────────────────────────
# Tokens: __LIB_IMPORTS__, __LIB_TOUCH__, __FW_NAME__, __LIB_NAME__.

def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


# Ktor 1.x's routing/application/response APIs live in a flat io.ktor.* package
# tree; the 2.0.0 rewrite moved every server-side piece under io.ktor.server.*
# (confirmed via each major's own first release notes/migration guide) -- the
# one genuine breaking-API-shape split this template needs to account for.
# 2.x and 3.x share the io.ktor.server.* shape for this minimal routing/JSON
# surface (3.x's own breaking changes are elsewhere -- new default engine
# config, kotlinx-io-based I/O -- none of which this Hello-World-plus-JSON app
# touches), so one template covers both.
_KTOR_MAIN_V1 = """\
package app

import io.ktor.application.*
import io.ktor.response.*
import io.ktor.routing.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
fun main() {
    __LIB_TOUCH__

    embeddedServer(Netty, port = 8000) {
        routing {
            get("/") {
                call.respondText(
                    "{" + esc("message") + ":" + esc("Hello World") + "}",
                    io.ktor.http.ContentType.Application.Json
                )
            }
            get("/version") {
                val v = versions()
                val body = "{" +
                    esc("language") + ":{" + esc("name") + ":" + esc("Kotlin") + "," +
                        esc("version") + ":" + esc(KotlinVersion.CURRENT.toString()) + "}," +
                    esc("framework") + ":{" + esc("name") + ":" + esc("__FW_NAME__") + "," +
                        esc("version") + ":" + esc(v.getProperty("framework.version", "unknown")) + "}," +
                    esc("library") + ":{" + esc("name") + ":" + esc("__LIB_NAME__") + "," +
                        esc("version") + ":" + esc(v.getProperty("library.version", "unknown")) + "}" +
                "}"
                call.respondText(body, io.ktor.http.ContentType.Application.Json)
            }
        }
    }.start(wait = true)
}
"""

_KTOR_MAIN_V2 = """\
package app

import io.ktor.server.application.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import io.ktor.server.response.*
import io.ktor.server.routing.*
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
fun main() {
    __LIB_TOUCH__

    embeddedServer(Netty, port = 8000) {
        routing {
            get("/") {
                call.respondText(
                    "{" + esc("message") + ":" + esc("Hello World") + "}",
                    io.ktor.http.ContentType.Application.Json
                )
            }
            get("/version") {
                val v = versions()
                val body = "{" +
                    esc("language") + ":{" + esc("name") + ":" + esc("Kotlin") + "," +
                        esc("version") + ":" + esc(KotlinVersion.CURRENT.toString()) + "}," +
                    esc("framework") + ":{" + esc("name") + ":" + esc("__FW_NAME__") + "," +
                        esc("version") + ":" + esc(v.getProperty("framework.version", "unknown")) + "}," +
                    esc("library") + ":{" + esc("name") + ":" + esc("__LIB_NAME__") + "," +
                        esc("version") + ":" + esc(v.getProperty("library.version", "unknown")) + "}" +
                "}"
                call.respondText(body, io.ktor.http.ContentType.Application.Json)
            }
        }
    }.start(wait = true)
}
"""

_KTOR_MAIN_BY_MAJOR = {"1": _KTOR_MAIN_V1}
_KTOR_MAIN_DEFAULT = _KTOR_MAIN_V2  # 2.x/3.x


def _ktor_main_tpl(fw_major: str) -> str:
    return _KTOR_MAIN_BY_MAJOR.get(fw_major, _KTOR_MAIN_DEFAULT)


_SPRING_BOOT_MAIN = """\
package app

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
@SpringBootApplication
@RestController
class Main {

    init {
        __LIB_TOUCH__
    }

    @GetMapping("/")
    fun root(): Map<String, Any> = linkedMapOf("message" to "Hello World")

    @GetMapping("/version")
    fun version(): Map<String, Any> {
        val v = versions()
        return linkedMapOf(
            "language" to linkedMapOf(
                "name" to "Kotlin",
                "version" to KotlinVersion.CURRENT.toString()
            ),
            "framework" to linkedMapOf(
                "name" to "__FW_NAME__",
                "version" to v.getProperty("framework.version", "unknown")
            ),
            "library" to linkedMapOf(
                "name" to "__LIB_NAME__",
                "version" to v.getProperty("library.version", "unknown")
            )
        )
    }
}

fun main(args: Array<String>) {
    runApplication<Main>(*args)
}
"""

# http4k's own canonical minimal usage (confirmed via its own
# UsageFromJava_undertow.java test fixture): HttpHandler = (Request) ->
# Response; routes(...)/bind wires path-based dispatch; asServer(Undertow(port))
# starts the embedded server. No annotations, no DI container.
_HTTP4K_MAIN = """\
package app

import org.http4k.core.HttpHandler
import org.http4k.core.Method.GET
import org.http4k.core.Response
import org.http4k.core.Status.Companion.OK
import org.http4k.routing.bind
import org.http4k.routing.routes
import org.http4k.server.Undertow
import org.http4k.server.asServer
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
fun main() {
    __LIB_TOUCH__

    val app: HttpHandler = routes(
        "/" bind GET to {
            Response(OK)
                .header("Content-Type", "application/json")
                .body("{" + esc("message") + ":" + esc("Hello World") + "}")
        },
        "/version" bind GET to {
            val v = versions()
            val body = "{" +
                esc("language") + ":{" + esc("name") + ":" + esc("Kotlin") + "," +
                    esc("version") + ":" + esc(KotlinVersion.CURRENT.toString()) + "}," +
                esc("framework") + ":{" + esc("name") + ":" + esc("__FW_NAME__") + "," +
                    esc("version") + ":" + esc(v.getProperty("framework.version", "unknown")) + "}," +
                esc("library") + ":{" + esc("name") + ":" + esc("__LIB_NAME__") + "," +
                    esc("version") + ":" + esc(v.getProperty("library.version", "unknown")) + "}" +
            "}"
            Response(OK)
                .header("Content-Type", "application/json")
                .body(body)
        }
    )

    app.asServer(Undertow(8000)).start()
}
"""

_MICRONAUT_MAIN = """\
package app

import io.micronaut.http.annotation.Controller
import io.micronaut.http.annotation.Get
import io.micronaut.runtime.Micronaut
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
@Controller("/")
class Main {

    init {
        __LIB_TOUCH__
    }

    @Get
    fun root(): Map<String, Any> = linkedMapOf("message" to "Hello World")

    @Get("/version")
    fun version(): Map<String, Any> {
        val v = versions()
        return linkedMapOf(
            "language" to linkedMapOf(
                "name" to "Kotlin",
                "version" to KotlinVersion.CURRENT.toString()
            ),
            "framework" to linkedMapOf(
                "name" to "__FW_NAME__",
                "version" to v.getProperty("framework.version", "unknown")
            ),
            "library" to linkedMapOf(
                "name" to "__LIB_NAME__",
                "version" to v.getProperty("library.version", "unknown")
            )
        )
    }
}

fun main(args: Array<String>) {
    Micronaut.run(Main::class.java, *args)
}
"""

_QUARKUS_MAIN = """\
package app

import __JAXRS_PKG__.GET
import __JAXRS_PKG__.Path
__LIB_IMPORTS__

""" + _VERSIONS_READ_HELPER + """
@Path("/")
class Main {

    init {
        __LIB_TOUCH__
    }

    @GET
    fun root(): Map<String, Any> = linkedMapOf("message" to "Hello World")

    @GET
    @Path("/version")
    fun version(): Map<String, Any> {
        val v = versions()
        return linkedMapOf(
            "language" to linkedMapOf(
                "name" to "Kotlin",
                "version" to KotlinVersion.CURRENT.toString()
            ),
            "framework" to linkedMapOf(
                "name" to "__FW_NAME__",
                "version" to v.getProperty("framework.version", "unknown")
            ),
            "library" to linkedMapOf(
                "name" to "__LIB_NAME__",
                "version" to v.getProperty("library.version", "unknown")
            )
        )
    }
}
"""

_APP_TPL = {
    "Ktor":        None,  # resolved via _ktor_main_tpl() below, era-dependent
    "Spring Boot": _SPRING_BOOT_MAIN,
    "http4k":      _HTTP4K_MAIN,
    "Micronaut":   _MICRONAUT_MAIN,
    "Quarkus":     _QUARKUS_MAIN,
}


def make_main_kt(fw_name: str, fw_major: str, lib_name: str, lib_ver: str = "") -> str:
    meta = LIB_META[lib_name]
    imports = "\n".join(f"import {imp}" for imp in meta["imports"])
    tpl = _ktor_main_tpl(fw_major) if fw_name == "Ktor" else _APP_TPL[fw_name]
    return _sub(
        tpl,
        LIB_IMPORTS = imports,
        LIB_TOUCH   = meta["touch"],
        FW_NAME     = fw_name,
        LIB_NAME    = lib_name,
        JAXRS_PKG   = _quarkus_jaxrs_pkg(fw_major) if fw_name == "Quarkus" else "",
    )


# ── build.gradle.kts generation ──────────────────────────────────────────────────

def _lib_dependency_line(lib_name: str, lib_resolved: str) -> str:
    if lib_name == "liboqs-kotlin":
        return '    implementation(files("libs/liboqs-java.jar"))\n'
    coord = _lib_coord(lib_name, lib_resolved)
    if not coord:
        return ""
    group, artifact = coord
    return f'    implementation("{group}:{artifact}:{lib_resolved}")\n'


def _gradle_build_kts(kotlin_resolved: str, jdk: str, fw_name: str, fw_major: str,
                       fw_resolved: str, lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_line(lib_name, lib_resolved)

    if fw_name == "Quarkus":
        return _gradle_build_kts_quarkus(kotlin_resolved, jdk, fw_major, fw_resolved, lib_dep)

    if fw_name == "Ktor":
        fw_deps = (
            f'    implementation("io.ktor:{_KTOR_ENGINE_ARTIFACT}:{fw_resolved}")\n'
            f'    implementation("io.ktor:{_KTOR_CORE_ARTIFACT}:{fw_resolved}")\n'
        )
    elif fw_name == "http4k":
        fw_deps = (
            f'    implementation("org.http4k:http4k-core:{fw_resolved}")\n'
            f'    implementation("org.http4k:http4k-server-undertow:{fw_resolved}")\n'
        )
    elif fw_name == "Micronaut":
        # No official io.micronaut.application Gradle plugin used (same
        # reasoning as Spring Boot below) -- the exact per-major BOM/parent
        # coordinate lang_java.py already established is imported directly
        # via Gradle's platform() notation, which reads a POM's own
        # <dependencyManagement> the same way a Maven <parent>/import would.
        # Annotation-processing MECHANISM genuinely differs by major -- see
        # _MICRONAUT_KSP_MAJORS' own comment for the full story (confirmed
        # via two real, compounding bugs: micronaut-inject-kotlin doesn't
        # exist before 4.0, and its 4.x/5.x jar is KSP-only, no kapt-
        # compatible Processor SPI at all).
        bom_group, bom_artifact = _micronaut_bom_coord(fw_major)
        serde_dep = "" if fw_major in _MICRONAUT_NO_SERDE_MAJORS else (
            '    implementation("io.micronaut.serde:micronaut-serde-jackson")\n'
        )
        logback_ver = "1.2.11" if fw_major == "1" else ""
        logback_line = (
            f'    runtimeOnly("ch.qos.logback:logback-classic:{logback_ver}")\n' if logback_ver
            else '    runtimeOnly("ch.qos.logback:logback-classic")\n'
        )
        if fw_major in _MICRONAUT_KSP_MAJORS:
            processor_dep = f'    ksp("io.micronaut:micronaut-inject-kotlin:{fw_resolved}")\n'
        else:
            processor_dep = f'    kapt("io.micronaut:micronaut-inject-java:{fw_resolved}")\n'
        fw_deps = (
            f'    implementation(platform("{bom_group}:{bom_artifact}:{fw_resolved}"))\n'
            f"{processor_dep}"
            '    implementation("io.micronaut:micronaut-http-server-netty")\n'
            f"{serde_dep}"
            f"{logback_line}"
        )
    else:  # Spring Boot
        # Deliberately NOT using the org.springframework.boot Gradle plugin /
        # io.spring.dependency-management plugin (which would need their own,
        # separately-resolved plugin versions per Boot major on top of Boot's
        # own version) -- a direct spring-boot-starter-web dependency resolves
        # its OWN transitive versions from its own POM's dependency management
        # regardless of whether a project also imports the parent BOM itself,
        # exactly the same as any other direct Maven/Gradle dependency. The
        # `application` plugin (used uniformly for both frameworks here) is
        # sufficient to produce a runnable install image without adding a
        # second build-tool-plugin-version axis to resolve and verify.
        fw_deps = (
            f'    implementation("org.springframework.boot:spring-boot-starter-web:{fw_resolved}")\n'
            f'    implementation("org.jetbrains.kotlin:kotlin-reflect:{kotlin_resolved}")\n'
            f'    implementation("com.fasterxml.jackson.module:jackson-module-kotlin:{_jackson_kotlin_version()}")\n'
        )

    extra_plugins = ""
    if fw_name == "Spring Boot":
        extra_plugins = f'    kotlin("plugin.spring") version "{kotlin_resolved}"\n'
    elif fw_name == "Micronaut":
        if fw_major in _MICRONAUT_KSP_MAJORS:
            ksp_resolved = _ksp_version(kotlin_resolved)
            extra_plugins = f'    id("com.google.devtools.ksp") version "{ksp_resolved}"\n'
        else:
            extra_plugins = f'    kotlin("kapt") version "{kotlin_resolved}"\n'

    kotlin_line = ".".join(kotlin_resolved.split(".")[:2])

    # application.mainClass (a Property<String>) was only added in Gradle
    # 6.4 -- the older `application` plugin only has `mainClassName` (a
    # plain String). Confirmed via a real build failure ("Unresolved
    # reference: mainClass") under this project's own Gradle 5.6.4 tier
    # (Kotlin 1.3-1.5). Every Kotlin line at or past the 1.6 tier already
    # uses Gradle 6.9.4+ (well past 6.4), so only 1.0-1.5 need the old form.
    if kotlin_line in _OLD_MAINCLASS_LINES:
        app_block = 'application {\n    mainClassName = "app.MainKt"\n}\n'
    else:
        app_block = 'application {\n    mainClass.set("app.MainKt")\n}\n'

    # kotlin { jvmToolchain(...) } is a Kotlin Gradle Plugin DSL function
    # that doesn't exist before KGP 1.7.0 -- confirmed via the identical
    # class of real build failure ("Unresolved reference: jvmToolchain")
    # under the 1.3 tier. Falls back to the older, universally-supported
    # per-task kotlinOptions.jvmTarget for every Kotlin line before 1.7 --
    # JDK 8 needs the legacy "1.8" string there (Gradle/Kotlin's own
    # historical JavaVersion.toString() convention), every later JDK just
    # uses its own plain number.
    if kotlin_line in _OLD_JVM_TARGET_LINES:
        jvm_target = "1.8" if jdk == "8" else jdk
        kotlin_block = (
            "tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile> {\n"
            f'    kotlinOptions.jvmTarget = "{jvm_target}"\n'
            "}\n"
        )
    else:
        kotlin_block = "kotlin {\n" f"    jvmToolchain({jdk})\n" "}\n"

    return (
        "plugins {\n"
        f'    kotlin("jvm") version "{kotlin_resolved}"\n'
        f"{extra_plugins}"
        "    application\n"
        "}\n"
        "\n"
        "repositories {\n"
        "    mavenCentral()\n"
        "}\n"
        "\n"
        "dependencies {\n"
        f"{fw_deps}"
        f"{lib_dep}"
        "}\n"
        "\n"
        f"{app_block}"
        "\n"
        f"{kotlin_block}"
    )


def _gradle_build_kts_quarkus(kotlin_resolved: str, jdk: str, fw_major: str,
                               fw_resolved: str, lib_dep: str) -> str:
    """Quarkus genuinely needs its own build-time bytecode-augmentation step
    to function at all (confirmed via lang_java.py's own Maven pom.xml,
    which already binds quarkus-maven-plugin's 'build' goal into the
    lifecycle, not just resteasy/jackson dependencies) -- unlike Spring Boot/
    Micronaut, this is not a convenience plugin to skip. Uses the real,
    official id("io.quarkus") Gradle plugin, released in exact lockstep with
    quarkus-bom's own version (confirmed on the Gradle Plugin Portal back to
    1.0.0.Final), so no separate plugin-version axis is introduced."""
    rest_deps = "".join(
        f'    implementation("{g}:{a}")\n' for g, a in _quarkus_rest_deps(fw_major)
    )
    return (
        "plugins {\n"
        f'    kotlin("jvm") version "{kotlin_resolved}"\n'
        f'    id("io.quarkus") version "{fw_resolved}"\n'
        "}\n"
        "\n"
        # An explicit version avoids Gradle's own default ("unspecified"),
        # which the io.quarkus plugin bakes verbatim into the uber-jar's own
        # filename (app-unspecified-runner.jar) -- confirmed via a real
        # build. "0.0.0" matches lang_java.py's own <version>0.0.0</version>
        # convention, making the resulting app-0.0.0-runner.jar predictable.
        'version = "0.0.0"\n'
        "\n"
        "repositories {\n"
        "    mavenCentral()\n"
        "}\n"
        "\n"
        "dependencies {\n"
        f'    implementation(enforcedPlatform("io.quarkus:quarkus-bom:{fw_resolved}"))\n'
        f"{rest_deps}"
        '    implementation("io.quarkus:quarkus-arc")\n'
        '    implementation("io.quarkus:quarkus-kotlin")\n'
        f"{lib_dep}"
        "}\n"
        "\n"
        # quarkus-bom manages org.jetbrains.kotlin:kotlin-stdlib's own
        # version too (confirmed via its own published POM) -- since it's
        # imported as enforcedPlatform (a genuine requirement, not a
        # convenience choice: Quarkus's build-time augmentation needs its
        # OWN managed versions authoritative for everything else it
        # controls), that silently overrides the kotlin("jvm") plugin's own
        # version for the STDLIB specifically, confirmed via a real build
        # where KotlinVersion.CURRENT reported 2.4.0 despite an explicit
        # "2.3.21" compiler-plugin declaration. Without this force, two
        # different tracked Kotlin lines could resolve to an IDENTICAL
        # runtime stdlib whenever quarkus-bom's own pin doesn't move between
        # them -- silently collapsing this registry's own Kotlin-version
        # axis for Quarkus specifically. resolutionStrategy.force() is
        # Gradle's own documented override mechanism for exactly this
        # situation (takes precedence over an enforced platform).
        "configurations.all {\n"
        "    resolutionStrategy.force(\n"
        f'        "org.jetbrains.kotlin:kotlin-stdlib:{kotlin_resolved}"\n'
        "    )\n"
        "}\n"
        "\n"
        "kotlin {\n"
        f"    jvmToolchain({jdk})\n"
        "}\n"
    )


def _settings_gradle_kts() -> str:
    return 'rootProject.name = "app"\n'


# ── Dockerfile generation ─────────────────────────────────────────────────────────

def make_dockerfile(kotlin_ver: str, fw_name: str, fw_major: str, lib_name: str, lib_ver: str) -> str:
    jdk, gradle_tag = _toolchain(kotlin_ver, fw_name)
    cache_bust = f'ARG PQC_COMBO_ID="{fw_name}-{fw_major}-{lib_name}@{lib_ver}"\n'
    needs_liboqs = lib_name == "liboqs-kotlin"

    # Quarkus's own build-time bytecode-augmentation step (the io.quarkus
    # Gradle plugin's quarkusBuild task) produces a different artifact
    # (app-0.0.0-runner.jar, an uber-jar per this project's quarkus.package.
    # jar.type=uber-jar setting -- confirmed live via a real build; without
    # an explicit `version` the filename becomes app-unspecified-runner.jar,
    # Gradle's own default project version baked verbatim into the name)
    # than every other framework's plain `gradle installDist` (an
    # application-plugin install directory) -- see registry notes for why
    # Quarkus alone needs its own official Gradle plugin.
    if fw_name == "Quarkus":
        build_cmd = "gradle quarkusBuild --no-daemon -x test\n"
        runtime_copy = "COPY --from=builder /build/build/app-0.0.0-runner.jar ./app.jar\n"
        cmd = 'CMD ["java", "-jar", "app.jar"]\n'
    else:
        build_cmd = "gradle installDist --no-daemon -x test\n"
        runtime_copy = "COPY --from=builder /build/build/install/app ./\n"
        cmd = 'CMD ["./bin/app"]\n'

    if not needs_liboqs:
        return (
            "# syntax=docker/dockerfile:1\n"
            f"FROM gradle:{gradle_tag} AS builder\n"
            "WORKDIR /build\n"
            f"{cache_bust}"
            "COPY settings.gradle.kts build.gradle.kts ./\n"
            "COPY src ./src\n"
            "RUN --mount=type=cache,id=gradle-cache,target=/home/gradle/.gradle,sharing=locked \\\n"
            f"    {build_cmd}"
            "\n"
            f"FROM eclipse-temurin:{jdk}-jre\n"
            "WORKDIR /app\n"
            f"{runtime_copy}"
            "EXPOSE 8000\n"
            f"{cmd}"
        )

    # liboqs-kotlin: liboqs-java has no Maven Central artifact at all (a real,
    # confirmed 404), so it must be built from source -- git-clone liboqs (the
    # C library) at a pinned tag, cmake+ninja build+install it (static .a, the
    # form liboqs-java's own Linux Maven profile links against), then git-clone
    # liboqs-java and `mvn package` it against that install to produce a single
    # jar with the compiled JNI .so embedded. Reuses lang_php.py's php-liboqs
    # multi-stage shape (build stage keeps the whole cmake/gcc/maven toolchain,
    # ~250MB+, needed only to PRODUCE the jar; final stage starts fresh and
    # copies over just the built application + jackson/etc via Gradle's own
    # installDist, discarding every build tool). The `maven:3-eclipse-temurin-
    # {jdk}` image is reused here (already used identically by lang_java.py) so
    # this stage needs no separate JDK install of its own.
    liboqs_stage = (
        f"FROM maven:3-eclipse-temurin-{jdk} AS liboqs-builder\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    cmake ninja-build gcc g++ git libssl-dev patchelf \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"RUN git clone --depth 1 --branch {_LIBOQS_C_TAG} \\\n"
        "    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \\\n"
        "    && cmake -S /tmp/liboqs -B /tmp/liboqs/build -DCMAKE_BUILD_TYPE=Release -GNinja \\\n"
        "    && cmake --build /tmp/liboqs/build --target install \\\n"
        "    && rm -rf /tmp/liboqs && ldconfig\n"
        f"RUN git clone --depth 1 --branch {_LIBOQS_JAVA_TAG} \\\n"
        "    https://github.com/open-quantum-safe/liboqs-java /tmp/liboqs-java \\\n"
        "    && cd /tmp/liboqs-java \\\n"
        "    && mvn -B -q package -Dmaven.test.skip=true \\\n"
        # liboqs-java's own native-maven-plugin link step (mvn package, linux
        # profile) produces a liboqs-jni.so that references CRYPTO_malloc
        # from OpenSSL as an undefined dynamic symbol, but never records
        # libcrypto.so.3 in its own NEEDED list -- confirmed via a real
        # runtime crash ("symbol lookup error ... undefined symbol:
        # CRYPTO_malloc") and readelf inspection showing only libc/ld-linux
        # as NEEDED entries despite CRYPTO_malloc appearing as an undefined
        # dynamic symbol. patchelf adds the missing NEEDED entry directly
        # (confirmed live: a patched copy loaded and generated a real
        # ML-KEM-768 keypair under eclipse-temurin's own JRE); OpenSSL 3.x's
        # stable-ABI guarantee is what makes this safe even though the
        # builder (Ubuntu 24.04, OpenSSL 3.0.13) and runtime (Ubuntu 26.04,
        # OpenSSL 3.5.5 as of this writing) ship different libcrypto minors.
        "    && jar xf target/liboqs-java.jar liboqs-jni.so \\\n"
        "    && patchelf --add-needed libcrypto.so.3 liboqs-jni.so \\\n"
        "    && jar uf target/liboqs-java.jar liboqs-jni.so \\\n"
        "    && cp target/liboqs-java.jar /tmp/liboqs-java.jar\n"
        "\n"
    )

    return (
        "# syntax=docker/dockerfile:1\n"
        f"{liboqs_stage}"
        f"FROM gradle:{gradle_tag} AS builder\n"
        "WORKDIR /build\n"
        f"{cache_bust}"
        "COPY --from=liboqs-builder /tmp/liboqs-java.jar ./libs/liboqs-java.jar\n"
        "COPY settings.gradle.kts build.gradle.kts ./\n"
        "COPY src ./src\n"
        "RUN --mount=type=cache,id=gradle-cache,target=/home/gradle/.gradle,sharing=locked \\\n"
        f"    {build_cmd}"
        "\n"
        f"FROM eclipse-temurin:{jdk}-jre\n"
        # liboqs-jni.so links dynamically against libcrypto (its own linker
        # config uses "-lcrypto", not a static OpenSSL) -- eclipse-temurin's
        # Ubuntu-based jre images don't bundle it by default, confirmed the
        # same way lang_php.py confirmed the OPPOSITE for php:{ver}-cli
        # (that image already ships libssl/libcrypto; this one doesn't).
        "RUN apt-get update && apt-get install -y --no-install-recommends libssl3 \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "WORKDIR /app\n"
        f"{runtime_copy}"
        "EXPOSE 8000\n"
        f"{cmd}"
    )


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write build.gradle.kts / settings.gradle.kts / src / Dockerfile for one
    image context. Returns False (and removes any stale directory) when a
    required Maven coordinate is confirmed absent. Returns False WITHOUT
    touching any existing directory when the lookup itself failed (network/
    rate-limit) -- same MavenLookupError-vs-None distinction lang_java.py
    established, reused verbatim here since resolution is delegated to it."""
    out = images_base / "kotlin" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    kotlin_resolved = _resolve("org.jetbrains.kotlin", "kotlin-stdlib", lang_ver)
    if kotlin_resolved is None:
        print(f"  [SKIP] Kotlin {lang_ver} not resolvable on Maven Central", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    try:
        if fw_name == "Ktor":
            # Never "latest patch of the major" here -- see _resolve_ktor()
            # and _KTOR_MINOR_KOTLIN_FLOOR: Ktor's own latest patches keep
            # bumping their required Kotlin compiler version, so the
            # resolved patch must stay bounded by the target Kotlin line.
            fw_resolved = _resolve_ktor(fw_major, lang_ver)
        elif fw_name == "http4k":
            # Same reasoning as Ktor, but http4k's own release-per-merge
            # cadence made a full per-minor floor table impractical -- see
            # _HTTP4K_CHECKPOINTS: resolves to a hand-verified exact
            # checkpoint version, never a live "latest patch" lookup.
            fw_resolved = _resolve_http4k(fw_major, lang_ver)
        else:
            fw_group, fw_artifact = _framework_anchor(fw_name, fw_major)
            fw_resolved = _resolve(fw_group, fw_artifact, fw_major)
    except MavenLookupError as exc:
        print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
        return False
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on Maven Central", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    if lib_name == "liboqs-kotlin":
        lib_resolved = lib_ver  # exact git tag version, never Maven-resolved
    elif lib_ver == "builtin":
        lib_resolved = "built-in"
    else:
        lib_coord = _lib_coord(lib_name, lib_ver)
        try:
            lib_resolved = _resolve(lib_coord[0], lib_coord[1], lib_ver)
        except MavenLookupError as exc:
            print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
            return False
        if lib_resolved is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on Maven Central", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    src_dir = out / "src" / "main" / "kotlin" / "app"
    res_dir = out / "src" / "main" / "resources"
    src_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    (src_dir / "Main.kt").write_text(
        make_main_kt(fw_name, fw_major, lib_name, lib_resolved), encoding="utf-8"
    )
    (res_dir / "versions.properties").write_text(
        _versions_properties(fw_resolved, lib_resolved), encoding="utf-8"
    )
    port_config = _PORT_CONFIG.get(fw_name)
    if port_config:
        (res_dir / "application.properties").write_text(port_config, encoding="utf-8")
    (out / "settings.gradle.kts").write_text(_settings_gradle_kts(), encoding="utf-8")

    jdk, _gradle_tag = _toolchain(lang_ver)
    (out / "build.gradle.kts").write_text(
        _gradle_build_kts(kotlin_resolved, jdk, fw_name, fw_major, fw_resolved, lib_name, lib_resolved),
        encoding="utf-8",
    )
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name, fw_major, lib_name, lib_resolved), encoding="utf-8"
    )
    return True
