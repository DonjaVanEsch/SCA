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
from pathlib import Path

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
    "1.5": ("8",  "5.6.4-jdk8"),
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


def _toolchain(kotlin_ver: str) -> tuple:
    return _TOOLCHAIN[kotlin_ver]


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
}


def _framework_anchor(fw_name: str, fw_major: str) -> tuple:
    if fw_name == "Ktor" and fw_major == "1":
        return FRAMEWORK_META["Ktor"]["anchor_v1"]
    return FRAMEWORK_META[fw_name]["anchor"]


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


# Ktor's own per-MINOR pinned kotlin_version/libs.versions.toml "kotlin"
# property, rounded down to this registry's own major.minor Kotlin
# granularity -- confirmed live (real per-tag gradle.properties/
# libs.versions.toml fetches) after a real docker build failure: resolving
# Ktor major "3" to its own absolute latest patch (3.5.2, built with Kotlin
# 2.3.21) against an OLDER target compiler like Kotlin 2.1.21 fails outright
# ("Module was compiled with an incompatible version of Kotlin. The binary
# version of its metadata is 2.3.0, expected version is 2.1.0") -- Kotlin
# binary metadata is forward-readable (an older jar built with an older
# Kotlin works fine under a newer compiler) but NOT backward-readable (a
# newer-built jar fails under an older compiler). Every other framework in
# this project's registries can safely resolve "always latest patch of the
# major" because none of them have this same per-patch compiler-version
# coupling; Ktor's own build is a Kotlin-Multiplatform project, so its
# artifacts are unusually sensitive to this. See _resolve_ktor() below.
_KTOR_MINOR_KOTLIN_FLOOR: dict = {
    "2.0": "1.6", "2.1": "1.7", "2.2": "1.7", "2.3": "1.8",
    "3.0": "2.0", "3.1": "2.1", "3.2": "2.1", "3.3": "2.2", "3.4": "2.3", "3.5": "2.3",
}


def _resolve_ktor(fw_major: str, kotlin_line: str) -> str | None:
    """Resolves Ktor major 1 the plain way (single bucket, no confirmed
    per-minor churn found there yet -- see registry notes' lower-confidence
    lifecycle-window caveat). Majors 2/3 pick the HIGHEST minor whose own
    pinned Kotlin requirement doesn't exceed the target Kotlin line, then
    resolve that minor to its own latest patch -- never simply "latest patch
    of the whole major", per _KTOR_MINOR_KOTLIN_FLOOR above."""
    if fw_major == "1":
        group, artifact = FRAMEWORK_META["Ktor"]["anchor_v1"]
        return _resolve(group, artifact, fw_major)

    group, artifact = FRAMEWORK_META["Ktor"]["anchor"]
    versions = lang_java._fetch_maven_versions(group, artifact)
    minors = sorted(
        {".".join(v.split(".")[:2]) for v in versions if v.startswith(fw_major + ".")},
        key=_ver_key,
    )
    target = _ver_key(kotlin_line)
    eligible = [m for m in minors if _ver_key(_KTOR_MINOR_KOTLIN_FLOOR.get(m, "0.0")) <= target]
    if not eligible:
        return None
    return _resolve(group, artifact, eligible[-1])


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

_APP_TPL = {
    "Ktor":        None,  # resolved via _ktor_main_tpl() below, era-dependent
    "Spring Boot": _SPRING_BOOT_MAIN,
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


def _gradle_build_kts(kotlin_resolved: str, jdk: str, fw_name: str, fw_resolved: str,
                       lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_line(lib_name, lib_resolved)

    if fw_name == "Ktor":
        fw_deps = (
            f'    implementation("io.ktor:{_KTOR_ENGINE_ARTIFACT}:{fw_resolved}")\n'
            f'    implementation("io.ktor:{_KTOR_CORE_ARTIFACT}:{fw_resolved}")\n'
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

    spring_plugin = (
        f'    kotlin("plugin.spring") version "{kotlin_resolved}"\n' if fw_name == "Spring Boot" else ""
    )

    return (
        "plugins {\n"
        f'    kotlin("jvm") version "{kotlin_resolved}"\n'
        f"{spring_plugin}"
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
        "application {\n"
        '    mainClass.set("app.MainKt")\n'
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
    jdk, gradle_tag = _toolchain(kotlin_ver)
    cache_bust = f'ARG PQC_COMBO_ID="{fw_name}-{fw_major}-{lib_name}@{lib_ver}"\n'

    if lib_name != "liboqs-kotlin":
        return (
            "# syntax=docker/dockerfile:1\n"
            f"FROM gradle:{gradle_tag} AS builder\n"
            "WORKDIR /build\n"
            f"{cache_bust}"
            "COPY settings.gradle.kts build.gradle.kts ./\n"
            "COPY src ./src\n"
            "RUN --mount=type=cache,id=gradle-cache,target=/home/gradle/.gradle,sharing=locked \\\n"
            "    gradle installDist --no-daemon -x test\n"
            "\n"
            f"FROM eclipse-temurin:{jdk}-jre\n"
            "WORKDIR /app\n"
            "COPY --from=builder /build/build/install/app ./\n"
            "EXPOSE 8000\n"
            'CMD ["./bin/app"]\n'
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
        "    gradle installDist --no-daemon -x test\n"
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
        "COPY --from=builder /build/build/install/app ./\n"
        "EXPOSE 8000\n"
        'CMD ["./bin/app"]\n'
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
        _gradle_build_kts(kotlin_resolved, jdk, fw_name, fw_resolved, lib_name, lib_resolved),
        encoding="utf-8",
    )
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name, fw_major, lib_name, lib_resolved), encoding="utf-8"
    )
    return True
