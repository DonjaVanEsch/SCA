"""
Java-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_java").

Required exports:
    LANGUAGE_ID   – str
    REGISTRY_FILE – str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool
"""

import json
import re
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "java"
REGISTRY_FILE = "registry java.json"


def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


# ── Framework metadata ────────────────────────────────────────────────────────
# Maven coordinate used both to resolve versions (maven-metadata.xml) and, for
# frameworks with a parent-POM/BOM style setup, as the <parent>/import target.
# "anchor" is (groupId, artifactId) of a single artifact whose version list is
# representative of the whole release train (all of a given framework's
# artifacts are released in lockstep against this number).
FRAMEWORK_META: dict = {
    "Spring Boot": {"anchor": ("org.springframework.boot", "spring-boot-starter-parent")},
    "Quarkus":     {"anchor": ("io.quarkus", "quarkus-bom")},
    "Vert.x":      {"anchor": ("io.vertx", "vertx-core")},
    "Helidon":     {"anchor": ("io.helidon.webserver", "helidon-webserver")},
    "Javalin":     {"anchor": ("io.javalin", "javalin")},
}

# Micronaut's parent-POM coordinate isn't lockstep with io.micronaut:micronaut-
# core's own version numbers, AND the groupId/mechanism itself changed across
# majors -- verified directly against Maven Central for each:
#   1.x -- io.micronaut:micronaut-parent has ZERO 1.x releases (starts at
#          2.0.0). 1.x projects used io.micronaut:micronaut-bom (which DOES
#          have 1.x releases from 1.0.0) imported via <dependencyManagement>
#          instead of a <parent> at all -- see _pom_micronaut_v1().
#   2.x -- io.micronaut:micronaut-parent (2.0.0 onward), used as <parent>.
#   3.x -- also io.micronaut:micronaut-parent (latest 3.10.9) -- a THIRD
#          distinct case, since 4.x+ moved to a different groupId entirely.
#   4.x/5.x -- io.micronaut.platform:micronaut-parent (latest 4.x is 4.10.16,
#          NOT micronaut-core's 4.10.25 -- asking Maven for
#          io.micronaut.platform:micronaut-parent:4.10.25 fails outright,
#          that version was never published under the platform groupId).
# So Micronaut's anchor must be resolved per-major-version against whichever
# coordinate is actually used in the generated pom.xml, not micronaut-core.
_MICRONAUT_PARENT_BY_MAJOR: dict = {
    "1": ("io.micronaut", "micronaut-bom"),
    "2": ("io.micronaut", "micronaut-parent"),
    "3": ("io.micronaut", "micronaut-parent"),
}
_MICRONAUT_PARENT_DEFAULT = ("io.micronaut.platform", "micronaut-parent")  # 4.x onward


def _micronaut_parent_coord(fw_major: str) -> tuple:
    return _MICRONAUT_PARENT_BY_MAJOR.get(fw_major, _MICRONAUT_PARENT_DEFAULT)


# micronaut-serde-jackson has zero 1.x/2.x/early-3.x releases (its own
# versioning starts 2022-03, after Micronaut 3.0's Sept-2021 release) --
# verified directly against Maven Central. Majors 1/2 always predate it
# (both fully released before serde existed), so they fall back to Jackson
# support bundled directly in micronaut-http/micronaut-core instead. Major 3
# keeps using serde since this registry always resolves to the LATEST 3.x
# patch, which postdates serde's release by years.
_MICRONAUT_NO_SERDE_MAJORS = frozenset({"1", "2"})


def _framework_anchor(fw_name: str, fw_major: str) -> tuple:
    if fw_name == "Micronaut":
        return _micronaut_parent_coord(fw_major)
    return FRAMEWORK_META[fw_name]["anchor"]

# blank/"touch" line: a real call into the library so it's provably loaded
# and exercised, not just declared as a dependency (mirrors Go's
# blank_import / Node's require() blank line). None means no Maven
# dependency (JCA is part of the JDK itself).
LIB_META: dict = {
    "JCA": {
        "coord": None,
        "imports": ["java.security.Security"],
        "touch": "Security.getProviders();",
    },
    "BouncyCastle": {
        "coord": ("org.bouncycastle", "bcprov-jdk18on"),
        "imports": ["java.security.Security", "org.bouncycastle.jce.provider.BouncyCastleProvider"],
        "touch": "Security.addProvider(new BouncyCastleProvider());",
    },
    "Tink": {
        "coord": ("com.google.crypto.tink", "tink"),
        "imports": ["com.google.crypto.tink.aead.AeadConfig"],
        # Version-aware -- see _tink_touch_line(), AeadConfig.register()
        # doesn't exist before 1.2.0. This default is only a fallback for
        # any caller that doesn't thread a lib_ver through.
        "touch": "try { AeadConfig.register(); } catch (Exception e) { /* exercised, ignore init failure */ }",
    },
    "Conscrypt": {
        "coord": ("org.conscrypt", "conscrypt-openjdk-uber"),
        "imports": ["java.security.Security", "org.conscrypt.Conscrypt"],
        "touch": "Security.addProvider(Conscrypt.newProvider());",
    },
}


# bcprov-jdk15on is the OLD, pre-rename, now-frozen (last release 1.70)
# artifact -- still resolvable on Maven Central today, kept as its own
# registry bucket ("1.70") specifically to offer an old-but-still-buildable
# version. Every other tracked bucket ("1.72", "1.79", "1") uses the current
# bcprov-jdk18on artifact.
_BC_LEGACY_BUCKET = "1.70"
_BC_LEGACY_COORD   = ("org.bouncycastle", "bcprov-jdk15on")


def _lib_coord(lib_name: str, lib_ver_bucket: str | None = None):
    if lib_name == "BouncyCastle" and lib_ver_bucket == _BC_LEGACY_BUCKET:
        return _BC_LEGACY_COORD
    return LIB_META[lib_name]["coord"]


# ── Maven Central version resolution ──────────────────────────────────────────
# Deliberately NOT using search.maven.org/solrsearch: verified live that its
# index lags the real repository by months to over a year (e.g. it topped out
# at bcprov-jdk18on 1.80 while 1.84 had long been published). The generated
# maven-metadata.xml under repo1.maven.org is Maven Central's own index and
# is authoritative.

_MAVEN_VERSIONS: dict = {}


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    except ValueError:
        return (0,)


def _fetch_maven_versions(group: str, artifact: str) -> list:
    cache_key = f"{group}:{artifact}"
    if cache_key in _MAVEN_VERSIONS:
        return _MAVEN_VERSIONS[cache_key]

    group_path = group.replace(".", "/")
    safe_artifact = urllib.parse.quote(artifact, safe="")
    url = f"https://repo1.maven.org/maven2/{group_path}/{safe_artifact}/maven-metadata.xml"
    versions = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        raw = [v.text for v in root.findall(".//versions/version") if v.text]
        # Accept a trailing stable-release marker (Maven Java projects use
        # several conventions for "this is GA, not a preview": Spring Boot
        # 1.x is ALWAYS suffixed ".RELEASE" (no clean version ever existed on
        # that line -- confirmed live against Maven Central), Quarkus 1.x/2.x
        # are ALWAYS suffixed ".Final" the same way. Excluding all suffixes
        # unconditionally (as this used to) makes an entire major
        # unresolvable, not just its prereleases -- caught when Spring Boot 1
        # and Quarkus 1/2 both failed with "not resolvable" despite being
        # real, current, installable coordinates. Still excludes genuine
        # prereleases (Alpha/Beta/CR/RC/M<n>/SNAPSHOT/...).
        versions = sorted(
            (v for v in raw if re.match(r"^\d+(\.\d+)*(\.(RELEASE|Final|GA))?$", v, re.IGNORECASE)),
            key=_ver_key,
        )
    except (URLError, ET.ParseError, OSError) as exc:
        print(f"  [WARN] Maven Central lookup failed for {group}:{artifact}: {exc}", flush=True)

    _MAVEN_VERSIONS[cache_key] = versions
    return versions


def _release_date(group: str, artifact: str, version: str) -> str | None:
    """release_date for one already-known version, e.g. for a newly detected
    major. maven-metadata.xml (used above) carries no per-version dates at
    all, unlike PyPI/npm/Packagist -- deliberately NOT falling back to
    search.maven.org for this either (see the module note above on why that
    index is avoided for version discovery); a plain HEAD request for the
    version's own POM and its Last-Modified header is a single small,
    authoritative request, only ever made for the one version a newly
    detected major actually resolved to, not for the whole history."""
    group_path = group.replace(".", "/")
    safe_artifact = urllib.parse.quote(artifact, safe="")
    url = (f"https://repo1.maven.org/maven2/{group_path}/{safe_artifact}/{version}/"
           f"{safe_artifact}-{version}.pom")
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            last_modified = resp.headers.get("Last-Modified")
        if not last_modified:
            return None
        return parsedate_to_datetime(last_modified).date().isoformat()
    except (URLError, OSError, ValueError, TypeError):
        return None


def _resolve(group: str, artifact: str, registry_ver: str) -> str | None:
    """Resolve a registry version like '3' to the latest matching release on
    Maven Central (e.g. '3' -> '3.5.16')."""
    versions = _fetch_maven_versions(group, artifact)

    prefix = registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in versions:
        return registry_ver

    return None


# ── Pre-fetch ─────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from Maven Central for all coordinates."""
    coords: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        for fv in fw.get("version", []):
            coords.add(_framework_anchor(fw["name"], fv["nr"]))
    for lib in lang_data.get("cryptography_libs", []):
        if lib.get("version") == "built-in":
            continue
        for lv in lib.get("version", []):
            coord = _lib_coord(lib["name"], lv["nr"])
            if coord:
                coords.add(coord)

    print("Fetching available versions from Maven Central ...")
    for group, artifact in sorted(coords):
        versions = _fetch_maven_versions(group, artifact)
        print(f"  {group}:{artifact}: {len(versions)} version(s) found")
    print()


# ── versions.properties (runtime version read) ────────────────────────────────
# Every framework/library API for reading "my own resolved version at
# runtime" turned out to have a sharp edge once actually checked: Quarkus
# ships an empty MANIFEST.MF since 3.1.0.Final; a shaded/uber jar merges
# every dependency's manifest into one, so Package.getImplementationVersion()
# on a class from an arbitrary dependency returns null; and each framework
# exposes its own version through a different, framework-specific API researched
# for this project.
#
# Sidestepped entirely: Maven pins an EXACT version per dependency (no range
# resolution the way npm/PyPI have), so whatever this generator resolves is
# exactly what gets installed -- there is no "did the resolver actually give
# me what I asked for" question the way there was for Node. So the resolved
# framework/library version strings are baked directly into a plain
# versions.properties resource file at generation time and read back with a
# single, uniform mechanism across all five frameworks.

def _versions_properties(fw_resolved: str, lib_resolved: str) -> str:
    return (
        f"framework.version={fw_resolved}\n"
        f"library.version={lib_resolved}\n"
    )


_VERSIONS_READ_HELPER = """\
	private static java.util.Properties versions() {
		java.util.Properties p = new java.util.Properties();
		try (java.io.InputStream in = Main.class.getResourceAsStream("/versions.properties")) {
			if (in != null) {
				p.load(in);
			}
		} catch (java.io.IOException e) {
			// leave empty -- /version endpoint reports "unknown" for missing keys
		}
		return p;
	}
"""


# ── App templates ─────────────────────────────────────────────────────────────
# Tokens: __LIB_IMPORTS__, __LIB_TOUCH__, __FW_NAME__, __LIB_NAME__.
# Every framework bakes the resolved framework/library version into
# versions.properties (see above) instead of reading it back from the
# classpath, so the app code never needs per-framework version-reading logic.

_SPRING_BOOT_MAIN = """\
package app;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
__LIB_IMPORTS__

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
@RestController
public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	public static void main(String[] args) {
		SpringApplication.run(Main.class, args);
	}

	@GetMapping("/")
	public Map<String, Object> root() {
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("message", "Hello World");
		return m;
	}

	@GetMapping("/version")
	public Map<String, Object> version() {
		Properties v = versions();

		Map<String, Object> language = new LinkedHashMap<>();
		language.put("name", "Java");
		language.put("version", System.getProperty("java.version"));

		Map<String, Object> framework = new LinkedHashMap<>();
		framework.put("name", "__FW_NAME__");
		framework.put("version", v.getProperty("framework.version", "unknown"));

		Map<String, Object> library = new LinkedHashMap<>();
		library.put("name", "__LIB_NAME__");
		library.put("version", v.getProperty("library.version", "unknown"));

		Map<String, Object> result = new LinkedHashMap<>();
		result.put("language", language);
		result.put("framework", framework);
		result.put("library", library);
		return result;
	}
}
"""

_QUARKUS_MAIN = """\
package app;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
__LIB_IMPORTS__

import __JAXRS_PKG__.GET;
import __JAXRS_PKG__.Path;

@Path("/")
public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	@GET
	public Map<String, Object> root() {
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("message", "Hello World");
		return m;
	}

	@GET
	@Path("/version")
	public Map<String, Object> version() {
		Properties v = versions();

		Map<String, Object> language = new LinkedHashMap<>();
		language.put("name", "Java");
		language.put("version", System.getProperty("java.version"));

		Map<String, Object> framework = new LinkedHashMap<>();
		framework.put("name", "__FW_NAME__");
		framework.put("version", v.getProperty("framework.version", "unknown"));

		Map<String, Object> library = new LinkedHashMap<>();
		library.put("name", "__LIB_NAME__");
		library.put("version", v.getProperty("library.version", "unknown"));

		Map<String, Object> result = new LinkedHashMap<>();
		result.put("language", language);
		result.put("framework", framework);
		result.put("library", library);
		return result;
	}
}
"""

_MICRONAUT_MAIN = """\
package app;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
__LIB_IMPORTS__

import io.micronaut.runtime.Micronaut;
import io.micronaut.http.annotation.Controller;
import io.micronaut.http.annotation.Get;

@Controller("/")
public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	public static void main(String[] args) {
		Micronaut.run(Main.class, args);
	}

	@Get
	public Map<String, Object> root() {
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("message", "Hello World");
		return m;
	}

	@Get("/version")
	public Map<String, Object> version() {
		Properties v = versions();

		Map<String, Object> language = new LinkedHashMap<>();
		language.put("name", "Java");
		language.put("version", System.getProperty("java.version"));

		Map<String, Object> framework = new LinkedHashMap<>();
		framework.put("name", "__FW_NAME__");
		framework.put("version", v.getProperty("framework.version", "unknown"));

		Map<String, Object> library = new LinkedHashMap<>();
		library.put("name", "__LIB_NAME__");
		library.put("version", v.getProperty("library.version", "unknown"));

		Map<String, Object> result = new LinkedHashMap<>();
		result.put("language", language);
		result.put("framework", framework);
		result.put("library", library);
		return result;
	}
}
"""

_VERTX_MAIN = """\
package app;

import java.util.Properties;
__LIB_IMPORTS__

import io.vertx.core.Vertx;
import io.vertx.core.json.JsonObject;
import io.vertx.ext.web.Router;

public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	public static void main(String[] args) {
		Properties v = versions();
		Vertx vertx = Vertx.vertx();
		Router router = Router.router(vertx);

		router.get("/").handler(ctx -> {
			JsonObject body = new JsonObject().put("message", "Hello World");
			ctx.response().putHeader("content-type", "application/json").end(body.encode());
		});

		router.get("/version").handler(ctx -> {
			JsonObject language = new JsonObject()
					.put("name", "Java")
					.put("version", System.getProperty("java.version"));
			JsonObject framework = new JsonObject()
					.put("name", "__FW_NAME__")
					.put("version", v.getProperty("framework.version", "unknown"));
			JsonObject library = new JsonObject()
					.put("name", "__LIB_NAME__")
					.put("version", v.getProperty("library.version", "unknown"));
			JsonObject body = new JsonObject()
					.put("language", language)
					.put("framework", framework)
					.put("library", library);
			ctx.response().putHeader("content-type", "application/json").end(body.encode());
		});

		vertx.createHttpServer().requestHandler(router).listen(8000);
	}
}
"""

# Vert.x 2.x predates the 3.0 core rewrite: packages are org.vertx.java.core.*
# (not io.vertx.core.*), there is no Router/vertx-web module at all (routing is
# done via the bundled RouteMatcher), and JsonObject uses put<Type> methods
# (putString/putObject) instead of the overloaded put() from 3.x onward --
# verified directly against the vertx-core 2.1.6 jar's class files on Maven
# Central, since Vert.x's own docs for this era are largely gone from the web.
_VERTX_MAIN_V2 = """\
package app;

import java.util.Properties;
__LIB_IMPORTS__

import org.vertx.java.core.Vertx;
import org.vertx.java.core.VertxFactory;
import org.vertx.java.core.http.HttpServerRequest;
import org.vertx.java.core.http.RouteMatcher;
import org.vertx.java.core.json.JsonObject;

public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	public static void main(String[] args) throws InterruptedException {
		Properties v = versions();
		Vertx vertx = VertxFactory.newVertx();
		RouteMatcher routeMatcher = new RouteMatcher();

		routeMatcher.get("/", (HttpServerRequest req) -> {
			JsonObject body = new JsonObject().putString("message", "Hello World");
			req.response().putHeader("content-type", "application/json").end(body.encode());
		});

		routeMatcher.get("/version", (HttpServerRequest req) -> {
			JsonObject language = new JsonObject()
					.putString("name", "Java")
					.putString("version", System.getProperty("java.version"));
			JsonObject framework = new JsonObject()
					.putString("name", "__FW_NAME__")
					.putString("version", v.getProperty("framework.version", "unknown"));
			JsonObject library = new JsonObject()
					.putString("name", "__LIB_NAME__")
					.putString("version", v.getProperty("library.version", "unknown"));
			JsonObject body = new JsonObject()
					.putObject("language", language)
					.putObject("framework", framework)
					.putObject("library", library);
			req.response().putHeader("content-type", "application/json").end(body.encode());
		});

		vertx.createHttpServer().requestHandler(routeMatcher).listen(8000);

		// Vert.x 2.x's event-loop/worker threads are daemon threads (unlike 3.x+'s
		// Netty-backed threads), so main() must block or the JVM exits immediately
		// after registering the handlers, before ever serving a request.
		new java.util.concurrent.CountDownLatch(1).await();
	}
}
"""

_VERTX_MAIN_BY_MAJOR = {
    "2": _VERTX_MAIN_V2,
}
_VERTX_MAIN_DEFAULT = _VERTX_MAIN  # 3.x onward


def _vertx_main_tpl(fw_major: str) -> str:
    return _VERTX_MAIN_BY_MAJOR.get(fw_major, _VERTX_MAIN_DEFAULT)


_HELIDON_COMMON_HEAD = """\
package app;

import java.util.Properties;
__LIB_IMPORTS__

import io.helidon.webserver.Routing;
import io.helidon.webserver.ServerConfiguration;
import io.helidon.webserver.WebServer;

public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	private static String esc(String s) {
		return "\\"" + s.replace("\\\\", "\\\\\\\\").replace("\\"", "\\\\\\"") + "\\"";
	}

	private static String obj(String... kv) {
		StringBuilder sb = new StringBuilder("{");
		for (int i = 0; i < kv.length; i += 2) {
			if (i > 0) sb.append(",");
			sb.append(esc(kv[i])).append(":").append(kv[i + 1]);
		}
		return sb.append("}").toString();
	}
"""

# Helidon SE's WebServer API has three incompatible shapes across the majors
# tracked here (verified against real code snippets in Helidon's own docs at
# each version's branch, then build-and-run verified in a container):
#   1.x -- static WebServer.create(routing), routing pre-built via
#          Routing.builder()....build(); startup blocks on a raw
#          CompletableFuture (no Single type existed yet).
#   2.x -- also WebServer.create(...), but takes an un-built Routing.Builder
#          directly; startup blocks via Helidon's own Single.await().
#   3.x/4.x -- fluent WebServer.builder().routing(...).build().start().
_HELIDON_MAIN_V1 = _HELIDON_COMMON_HEAD + """\
	public static void main(String[] args) throws Exception {
		Properties v = versions();

		String rootBody = obj("message", esc("Hello World"));
		String versionBody = obj(
				"language", obj("name", esc("Java"), "version", esc(System.getProperty("java.version"))),
				"framework", obj("name", esc("__FW_NAME__"), "version", esc(v.getProperty("framework.version", "unknown"))),
				"library", obj("name", esc("__LIB_NAME__"), "version", esc(v.getProperty("library.version", "unknown")))
		);

		Routing routing = Routing.builder()
				.get("/", (req, res) -> res.send(rootBody))
				.get("/version", (req, res) -> res.send(versionBody))
				.build();

		WebServer.create(ServerConfiguration.builder().port(8000), routing)
				.start()
				.toCompletableFuture()
				.get();
	}
}
"""

_HELIDON_MAIN_V2 = _HELIDON_COMMON_HEAD + """\
	public static void main(String[] args) {
		Properties v = versions();

		String rootBody = obj("message", esc("Hello World"));
		String versionBody = obj(
				"language", obj("name", esc("Java"), "version", esc(System.getProperty("java.version"))),
				"framework", obj("name", esc("__FW_NAME__"), "version", esc(v.getProperty("framework.version", "unknown"))),
				"library", obj("name", esc("__LIB_NAME__"), "version", esc(v.getProperty("library.version", "unknown")))
		);

		Routing.Builder routing = Routing.builder()
				.get("/", (req, res) -> res.send(rootBody))
				.get("/version", (req, res) -> res.send(versionBody));

		WebServer.create(ServerConfiguration.builder().port(8000), routing)
				.start()
				.await();
	}
}
"""

_HELIDON_MAIN_V3 = """\
package app;

import java.util.Properties;
__LIB_IMPORTS__

import io.helidon.webserver.WebServer;

public class Main {

	static {
		__LIB_TOUCH__
	}

__VERSIONS_HELPER__
	private static String esc(String s) {
		return "\\"" + s.replace("\\\\", "\\\\\\\\").replace("\\"", "\\\\\\"") + "\\"";
	}

	private static String obj(String... kv) {
		StringBuilder sb = new StringBuilder("{");
		for (int i = 0; i < kv.length; i += 2) {
			if (i > 0) sb.append(",");
			sb.append(esc(kv[i])).append(":").append(kv[i + 1]);
		}
		return sb.append("}").toString();
	}

	public static void main(String[] args) {
		Properties v = versions();

		String rootBody = obj("message", esc("Hello World"));
		String versionBody = obj(
				"language", obj("name", esc("Java"), "version", esc(System.getProperty("java.version"))),
				"framework", obj("name", esc("__FW_NAME__"), "version", esc(v.getProperty("framework.version", "unknown"))),
				"library", obj("name", esc("__LIB_NAME__"), "version", esc(v.getProperty("library.version", "unknown")))
		);

		WebServer server = WebServer.builder()
				.port(8000)
				.routing(routing -> routing
						.get("/", (req, res) -> res.send(rootBody))
						.get("/version", (req, res) -> res.send(versionBody)))
				.build();
		server.start();
	}
}
"""

_HELIDON_MAIN_BY_MAJOR = {
    "1": _HELIDON_MAIN_V1,
    "2": _HELIDON_MAIN_V2,
}
_HELIDON_MAIN_DEFAULT = _HELIDON_MAIN_V3  # 3.x/4.x


def _helidon_main_tpl(fw_major: str) -> str:
    return _HELIDON_MAIN_BY_MAJOR.get(fw_major, _HELIDON_MAIN_DEFAULT)


# Quarkus's JAX-RS namespace and REST extension coordinates both depend on
# major version -- see the "Quarkus" notes in registry java.json for the
# verified facts behind this split.
_QUARKUS_JAXRS_PKG_BY_MAJOR = {
    "1": "javax.ws.rs",
    "2": "javax.ws.rs",
}
_QUARKUS_JAXRS_PKG_DEFAULT = "jakarta.ws.rs"  # 3.x onward


def _quarkus_jaxrs_pkg(fw_major: str) -> str:
    return _QUARKUS_JAXRS_PKG_BY_MAJOR.get(fw_major, _QUARKUS_JAXRS_PKG_DEFAULT)


_QUARKUS_REST_DEPS_BY_MAJOR = {
    "1": [("io.quarkus", "quarkus-resteasy"), ("io.quarkus", "quarkus-resteasy-jackson")],
    "2": [("io.quarkus", "quarkus-resteasy"), ("io.quarkus", "quarkus-resteasy-jackson")],
}
_QUARKUS_REST_DEPS_DEFAULT = [("io.quarkus", "quarkus-rest-jackson")]  # 3.x onward


def _quarkus_rest_deps(fw_major: str) -> list:
    return _QUARKUS_REST_DEPS_BY_MAJOR.get(fw_major, _QUARKUS_REST_DEPS_DEFAULT)


# Javalin 3.x-6.x: `Javalin` implements the routing interface directly, so
# app.get(path, handler) works immediately after Javalin.create() --
# confirmed live via javap/source inspection of 5.x and 6.0.0.
_JAVALIN_MAIN_LEGACY = """\
package app;

import java.util.Properties;
import java.util.Map;
__LIB_IMPORTS__

import io.javalin.Javalin;

public class Main {

	static {
		__LIB_TOUCH__
	}

	// Map.of(...) is Java 9+ only -- Javalin 3/4/5 are still tracked as
	// JDK 8-compatible, so this can't use it (confirmed via a real
	// docker build: "cannot find symbol" on javac 8 for Map.of).
	private static Map<String, Object> mapOf(Object... kv) {
		Map<String, Object> m = new java.util.LinkedHashMap<>();
		for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
		return m;
	}

__VERSIONS_HELPER__
	public static void main(String[] args) {
		Properties v = versions();
		Javalin app = Javalin.create().start(8000);

		app.get("/", ctx -> ctx.json(mapOf("message", "Hello World")));

		app.get("/version", ctx -> ctx.json(mapOf(
				"language", mapOf("name", "Java", "version", System.getProperty("java.version")),
				"framework", mapOf("name", "__FW_NAME__", "version", v.getProperty("framework.version", "unknown")),
				"library", mapOf("name", "__LIB_NAME__", "version", v.getProperty("library.version", "unknown"))
		)));
	}
}
"""

# Javalin 7.x removed that direct interface implementation -- confirmed the
# hard way: app.get(...) fails to compile ("cannot find symbol") on 7.2.2.
# Routes must be registered inside the Javalin.create(cfg -> ...) config
# consumer instead, via cfg.routes.get(...).
_JAVALIN_MAIN_V7 = """\
package app;

import java.util.Properties;
import java.util.Map;
__LIB_IMPORTS__

import io.javalin.Javalin;

public class Main {

	static {
		__LIB_TOUCH__
	}

	// Kept in sync with the legacy template's own JDK 8-safe helper (see its
	// comment) even though 7.x itself only tracks JDK 21+ -- avoids silently
	// reintroducing the same Map.of() bug if 7.x's floor is ever lowered.
	private static Map<String, Object> mapOf(Object... kv) {
		Map<String, Object> m = new java.util.LinkedHashMap<>();
		for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
		return m;
	}

__VERSIONS_HELPER__
	public static void main(String[] args) {
		Properties v = versions();
		Javalin.create(cfg -> {
			cfg.routes.get("/", ctx -> ctx.json(mapOf("message", "Hello World")));
			cfg.routes.get("/version", ctx -> ctx.json(mapOf(
					"language", mapOf("name", "Java", "version", System.getProperty("java.version")),
					"framework", mapOf("name", "__FW_NAME__", "version", v.getProperty("framework.version", "unknown")),
					"library", mapOf("name", "__LIB_NAME__", "version", v.getProperty("library.version", "unknown"))
			)));
		}).start(8000);
	}
}
"""

_JAVALIN_MAIN_BY_MAJOR = {
    "3": _JAVALIN_MAIN_LEGACY,
    "4": _JAVALIN_MAIN_LEGACY,
    "5": _JAVALIN_MAIN_LEGACY,
    "6": _JAVALIN_MAIN_LEGACY,
}
_JAVALIN_MAIN_DEFAULT = _JAVALIN_MAIN_V7  # 7.x onward


def _javalin_main_tpl(fw_major: str) -> str:
    return _JAVALIN_MAIN_BY_MAJOR.get(fw_major, _JAVALIN_MAIN_DEFAULT)


_APP_TPL = {
    "Spring Boot": _SPRING_BOOT_MAIN,
    "Quarkus":     _QUARKUS_MAIN,
    "Micronaut":   _MICRONAUT_MAIN,
    "Vert.x":      _VERTX_MAIN,
}


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


# AeadConfig.register() doesn't exist before Tink 1.2.0 -- confirmed live by
# decompiling the real published jars: 1.0.0/1.1.0 only expose init()/
# registerStandardKeyTypes(), 1.2.0 is the first to add register() (a
# real "cannot find symbol" Micronaut/Tink 1.0-1.1 compile failure surfaced
# this). registerStandardKeyTypes() already exists on every tracked version,
# so it's the right pre-1.2 substitute, not a different codepath.
_TINK_REGISTER_FROM = (1, 2)


def _tink_touch_line(lib_ver: str) -> str:
    method = "register" if _parse(lib_ver) >= _TINK_REGISTER_FROM else "registerStandardKeyTypes"
    return f"try {{ AeadConfig.{method}(); }} catch (Exception e) {{ /* exercised, ignore init failure */ }}"


def make_main_java(fw_name: str, fw_major: str, lib_name: str, lib_ver: str = "") -> str:
    meta = LIB_META[lib_name]
    imports = "\n".join(f"import {imp};" for imp in meta["imports"])
    touch = _tink_touch_line(lib_ver) if lib_name == "Tink" and lib_ver else meta["touch"]
    if fw_name == "Helidon":
        tpl = _helidon_main_tpl(fw_major)
    elif fw_name == "Vert.x":
        tpl = _vertx_main_tpl(fw_major)
    elif fw_name == "Javalin":
        tpl = _javalin_main_tpl(fw_major)
    else:
        tpl = _APP_TPL[fw_name]
    return _sub(
        tpl,
        LIB_IMPORTS      = imports,
        LIB_TOUCH        = touch,
        FW_NAME          = fw_name,
        LIB_NAME         = lib_name,
        VERSIONS_HELPER  = _VERSIONS_READ_HELPER,
        JAXRS_PKG        = _quarkus_jaxrs_pkg(fw_major) if fw_name == "Quarkus" else "",
    )


# ── pom.xml generation ────────────────────────────────────────────────────────

_TINK_PROTOBUF_VERSIONS: dict = {}


def _tink_protobuf_java_version(tink_resolved: str) -> str | None:
    """Tink declares protobuf-java as a required (non-optional) transitive
    dependency pinned via its own POM's ${protobuf-java.version} property.
    Explicitly re-declaring that exact version as a direct dependency in the
    generated pom.xml guarantees Maven resolves it correctly regardless of
    anything deeper in a framework's own dependency tree -- found necessary
    after a real NoClassDefFoundError (`com/google/protobuf/RuntimeVersion
    $RuntimeDomain`, a class that does genuinely exist in the version Tink
    asks for) surfaced with Micronaut specifically; Spring Boot + Tink had
    already been verified working without this, narrowing the problem to
    something version-resolution- or shading-related rather than Tink
    itself being broken. Fetched dynamically (not hardcoded) so this stays
    correct if a future Tink bucket changes its required protobuf version.
    """
    if tink_resolved in _TINK_PROTOBUF_VERSIONS:
        return _TINK_PROTOBUF_VERSIONS[tink_resolved]

    url = f"https://repo1.maven.org/maven2/com/google/crypto/tink/tink/{tink_resolved}/tink-{tink_resolved}.pom"
    version = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            pom_text = resp.read().decode("utf-8")
        match = re.search(r"<protobuf-java\.version>([^<]+)</protobuf-java\.version>", pom_text)
        if match:
            version = match.group(1)
    except (URLError, OSError) as exc:
        print(f"  [WARN] Could not read Tink {tink_resolved}'s own protobuf-java version: {exc}", flush=True)

    _TINK_PROTOBUF_VERSIONS[tink_resolved] = version
    return version


def _lib_dependency_xml(lib_name: str, lib_resolved: str) -> str:
    # lib_resolved doubles as the bucket hint here: for the exact-pinned
    # buckets ("1.70", "1.72", "1.79") the resolved version IS the bucket
    # name (Maven's exact-match resolution), so this correctly selects
    # bcprov-jdk15on only for "1.70" without needing the bucket separately.
    coord = _lib_coord(lib_name, lib_resolved)
    if not coord:
        return ""
    group, artifact = coord
    xml = (
        "    <dependency>\n"
        f"      <groupId>{group}</groupId>\n"
        f"      <artifactId>{artifact}</artifactId>\n"
        f"      <version>{lib_resolved}</version>\n"
        "    </dependency>\n"
    )
    if lib_name == "Tink":
        protobuf_ver = _tink_protobuf_java_version(lib_resolved)
        if protobuf_ver:
            xml += (
                "    <dependency>\n"
                "      <groupId>com.google.protobuf</groupId>\n"
                "      <artifactId>protobuf-java</artifactId>\n"
                f"      <version>{protobuf_ver}</version>\n"
                "    </dependency>\n"
            )
    return xml


# Pinning an explicit maven-compiler-plugin version is required, not
# cosmetic, for every framework whose pom has no <parent>/pluginManagement
# to supply one -- found via a real failing build, not anticipated. Root
# cause: when a plugin has no explicit version, Maven falls back to
# whatever default binding its OWN bundled maven-core ships with, and that
# default varies by Maven's OWN version -- confirmed by running 'mvn
# --version' inside every tracked JDK's 'maven:3-eclipse-temurin-{jdk}'
# builder image: JDK 17/19/20/22/23/24's images bundle Maven 3.9.x, but
# JDK 18's bundles the OUTLIER-OLD Maven 3.8.6, which falls back to the
# ancient maven-compiler-plugin 3.1 default. That old plugin predates
# support for the <maven.compiler.release> property entirely (added in
# 3.6+), so it silently ignores this pom's own release setting and falls
# back to ITS OWN hardcoded ancient default (source/target 1.5) -- which
# newer javac versions have since removed support for entirely, producing
# "Source option 5 is no longer supported. Use 7 or later." on JDK 18
# specifically, even though the pom explicitly requests JDK 18. Frameworks
# with a <parent> that manages plugin versions (Spring Boot via
# spring-boot-starter-parent) are immune regardless of which Maven is
# bundled; Quarkus/Vert.x/Helidon/Micronaut-v1 have no such parent and are
# NOT immune, so all four need this pin. 3.13.0 is a well-established,
# widely-used stable release (2023) -- picked over the newest 3.14/3.15/
# 4.0.0-beta to avoid any risk of ITS OWN edge cases on this project's
# oldest tracked JDKs, while still being new enough to correctly honor
# maven.compiler.release for JDK 25.
_COMPILER_PLUGIN = """\
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
      </plugin>
"""


# Excluding META-INF/*.SF|.DSA|.RSA is required, not optional, whenever any
# dependency's own jar is signed (e.g. BouncyCastle signs its jars) --
# shading merges that jar's contents into the uber-jar without updating its
# now-stale signature files, and the JVM refuses to load ANY class from the
# result at runtime with "SecurityException: Invalid signature file digest
# for Manifest main attributes". Confirmed via a real failing build+run
# before adding this filter, not applied speculatively.
#
# ServicesResourceTransformer is required for Micronaut 1.x/2.x specifically
# (harmless no-op for the others) -- confirmed by downloading the actual
# micronaut-http-server-netty-2.5.13.jar and finding its bean registrations
# listed in a plain java.util.ServiceLoader-style file,
# META-INF/services/io.micronaut.inject.BeanDefinitionReference (one shared
# text file, multiple class names, one per line). Shading multiple jars that
# each carry their OWN copy of that exact path silently keeps only the LAST
# one seen instead of merging them -- observed live as a real failure:
# Micronaut 2.5.13 started with zero exceptions but logged "No bean
# candidates found for type: interface io.micronaut.runtime.EmbeddedApplication"
# / "No embedded container found. Running as CLI application" -- the HTTP
# server's own bean-definition file had been clobbered, so Netty never
# started at all despite a completely clean-looking startup log (a repeat of
# the Helidon port lesson: "started with no errors" is not "works" -- read
# what the log actually says). Micronaut 3.x+ moved to a different, per-bean-
# file registration scheme specifically immune to this (confirmed by
# downloading micronaut-http-server-netty-4.10.16.jar: each bean gets its
# own uniquely-named, separate file under META-INF/micronaut/<interface>/,
# so there's nothing to clobber) -- which is why this went unnoticed until
# 1.x/2.x were added.
_SHADE_PLUGIN = """\
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-shade-plugin</artifactId>
        <version>3.5.1</version>
        <executions>
          <execution>
            <phase>package</phase>
            <goals><goal>shade</goal></goals>
            <configuration>
              <filters>
                <filter>
                  <artifact>*:*</artifact>
                  <excludes>
                    <exclude>META-INF/*.SF</exclude>
                    <exclude>META-INF/*.DSA</exclude>
                    <exclude>META-INF/*.RSA</exclude>
                  </excludes>
                </filter>
              </filters>
              <transformers>
                <transformer implementation="org.apache.maven.plugins.shade.resource.ManifestResourceTransformer">
                  <mainClass>app.Main</mainClass>
                </transformer>
                <transformer implementation="org.apache.maven.plugins.shade.resource.ServicesResourceTransformer" />
              </transformers>
            </configuration>
          </execution>
        </executions>
      </plugin>
"""


def _pom_spring_boot(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{fw_resolved}</version>
  </parent>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <java.version>{jdk_ver}</java.version>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _pom_quarkus(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str,
                 fw_major: str = "3") -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    rest_deps = "".join(
        f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n    </dependency>\n"
        for g, a in _quarkus_rest_deps(fw_major)
    )
    # Renamed in Quarkus 3.0: pre-3.x uses quarkus.package.type, 3.x+ uses quarkus.package.jar.type
    package_prop = "quarkus.package.type" if fw_major in ("1", "2") else "quarkus.package.jar.type"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.release>{jdk_ver}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <quarkus.platform.version>{fw_resolved}</quarkus.platform.version>
    <{package_prop}>uber-jar</{package_prop}>
  </properties>

  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>io.quarkus</groupId>
        <artifactId>quarkus-bom</artifactId>
        <version>${{quarkus.platform.version}}</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>

  <dependencies>
{rest_deps}    <dependency>
      <groupId>io.quarkus</groupId>
      <artifactId>quarkus-arc</artifactId>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
{_COMPILER_PLUGIN}      <plugin>
        <groupId>io.quarkus</groupId>
        <artifactId>quarkus-maven-plugin</artifactId>
        <version>${{quarkus.platform.version}}</version>
        <extensions>true</extensions>
        <executions>
          <execution>
            <goals>
              <goal>build</goal>
            </goals>
          </execution>
        </executions>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _pom_micronaut(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str,
                   parent_group: str = "io.micronaut.platform", fw_major: str = "4") -> str:
    if fw_major == "1":
        return _pom_micronaut_v1(jdk_ver, fw_resolved, lib_name, lib_resolved)

    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    serde_dep = "" if fw_major in _MICRONAUT_NO_SERDE_MAJORS else (
        "    <dependency>\n"
        "      <groupId>io.micronaut.serde</groupId>\n"
        "      <artifactId>micronaut-serde-jackson</artifactId>\n"
        "    </dependency>\n"
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>{parent_group}</groupId>
    <artifactId>micronaut-parent</artifactId>
    <version>{fw_resolved}</version>
  </parent>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <jdk.version>{jdk_ver}</jdk.version>
    <release.version>{jdk_ver}</release.version>
  </properties>

  <dependencies>
    <dependency>
      <groupId>io.micronaut</groupId>
      <artifactId>micronaut-http-server-netty</artifactId>
    </dependency>
{serde_dep}    <dependency>
      <groupId>ch.qos.logback</groupId>
      <artifactId>logback-classic</artifactId>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
{_SHADE_PLUGIN}    </plugins>
  </build>
</project>
"""


# Micronaut 1.x has no <parent> at all (io.micronaut:micronaut-parent didn't
# exist yet) -- projects imported io.micronaut:micronaut-bom via
# <dependencyManagement> instead, which means annotation processing
# (micronaut-inject-java, normally pre-wired by the parent POM for every
# other tracked major) must be configured manually on the compiler plugin.
def _pom_micronaut_v1(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.source>{jdk_ver}</maven.compiler.source>
    <maven.compiler.target>{jdk_ver}</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <micronaut.version>{fw_resolved}</micronaut.version>
  </properties>

  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>io.micronaut</groupId>
        <artifactId>micronaut-bom</artifactId>
        <version>${{micronaut.version}}</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>

  <dependencies>
    <dependency>
      <groupId>io.micronaut</groupId>
      <artifactId>micronaut-http-server-netty</artifactId>
    </dependency>
    <dependency>
      <groupId>io.micronaut</groupId>
      <artifactId>micronaut-inject-java</artifactId>
      <scope>provided</scope>
    </dependency>
    <dependency>
      <groupId>ch.qos.logback</groupId>
      <artifactId>logback-classic</artifactId>
      <version>1.2.11</version>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <configuration>
          <annotationProcessorPaths>
            <path>
              <groupId>io.micronaut</groupId>
              <artifactId>micronaut-inject-java</artifactId>
              <version>{fw_resolved}</version>
            </path>
          </annotationProcessorPaths>
        </configuration>
      </plugin>
{_SHADE_PLUGIN}    </plugins>
  </build>
</project>
"""


def _pom_vertx(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str,
               fw_major: str = "4") -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    # vertx-web was introduced at the 3.0 core rewrite -- it has no 1.x/2.x
    # releases at all (confirmed against Maven Central), so 2.x routes via
    # the RouteMatcher bundled in vertx-core itself instead (see _VERTX_MAIN_V2).
    web_dep = "" if fw_major == "2" else (
        "    <dependency>\n"
        "      <groupId>io.vertx</groupId>\n"
        "      <artifactId>vertx-web</artifactId>\n"
        f"      <version>{fw_resolved}</version>\n"
        "    </dependency>\n"
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.release>{jdk_ver}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>io.vertx</groupId>
      <artifactId>vertx-core</artifactId>
      <version>{fw_resolved}</version>
    </dependency>
{web_dep}{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
{_COMPILER_PLUGIN}{_SHADE_PLUGIN}    </plugins>
  </build>
</project>
"""


def _pom_helidon(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.release>{jdk_ver}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>io.helidon.webserver</groupId>
      <artifactId>helidon-webserver</artifactId>
      <version>{fw_resolved}</version>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
{_COMPILER_PLUGIN}{_SHADE_PLUGIN}    </plugins>
  </build>
</project>
"""


# Javalin does NOT bundle a JSON object mapper of its own -- ctx.json(...)
# throws a real HTTP 500 at request time ("It looks like you don't have an
# object mapper configured") unless jackson-databind is added explicitly,
# confirmed via a real docker run (not just a build). slf4j-simple is added
# too since Javalin logs a warning without any SLF4J binding present.
def _pom_javalin(jdk_ver: str, fw_resolved: str, lib_name: str, lib_resolved: str) -> str:
    lib_dep = _lib_dependency_xml(lib_name, lib_resolved)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>com.pqc</groupId>
  <artifactId>app</artifactId>
  <version>0.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.release>{jdk_ver}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>io.javalin</groupId>
      <artifactId>javalin</artifactId>
      <version>{fw_resolved}</version>
    </dependency>
    <dependency>
      <groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId>
      <version>2.21.2</version>
    </dependency>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-simple</artifactId>
      <version>2.0.16</version>
    </dependency>
{lib_dep}  </dependencies>

  <build>
    <finalName>app</finalName>
    <plugins>
{_COMPILER_PLUGIN}{_SHADE_PLUGIN}    </plugins>
  </build>
</project>
"""


_POM_TPL = {
    "Spring Boot": _pom_spring_boot,
    "Quarkus":     _pom_quarkus,
    "Micronaut":   _pom_micronaut,
    "Vert.x":      _pom_vertx,
    "Helidon":     _pom_helidon,
    "Javalin":     _pom_javalin,
}

# Final jar filename Maven produces under target/, per framework's packaging
# convention (all use finalName=app in their <build> section above; Quarkus's
# uber-jar packaging appends "-runner").
_JAR_FILENAME = {
    "Spring Boot": "app.jar",
    "Quarkus":     "app-runner.jar",
    "Micronaut":   "app.jar",
    "Vert.x":      "app.jar",
    "Helidon":     "app.jar",
    "Javalin":     "app.jar",
}

# HTTP port each framework defaults to, and the property/file needed to
# override it to this project's standard 8000. None means no config needed
# (Vert.x/Helidon/Javalin bind the port programmatically in Main.java instead).
_PORT_CONFIG = {
    "Spring Boot": "server.port=8000\n",
    "Quarkus":     "quarkus.http.port=8000\n",
    "Micronaut":   "micronaut.server.port=8000\n",
    "Vert.x":      None,
    "Helidon":     None,
    "Javalin":     None,
}


# ── Dockerfile generation ─────────────────────────────────────────────────────
# Multi-stage: `maven:3-eclipse-temurin-{jdk}` (Maven + matching JDK bundled by
# Adoptium/the official Maven image -- avoids apt-installing Maven by hand
# into a bare temurin image) builds the jar, then a slim `eclipse-temurin:
# {jdk}-jre-jammy` runs it. Both stages use the Ubuntu/glibc "-jammy" tag,
# not Alpine: eclipse-temurin has no "-slim" tags at all (that's a Debian/apt
# convention from the old, unmaintained openjdk Docker Official Image, never
# adopted by Adoptium's replacement), and Conscrypt's native library is
# glibc-linked, so Alpine/musl is avoided project-wide to sidestep a
# suspected (not yet empirically confirmed) UnsatisfiedLinkError.

# JDK 23/24 moved to Ubuntu 'noble' as eclipse-temurin's default base -- verified
# live against Docker Hub's registry API that no '-jre-jammy' variant exists at
# all for these two versions (only '-jre-noble', alongside alpine/ubi/windows
# variants), while every other tracked JDK (8-22, 25) still has a live
# '-jre-jammy' tag. Same class of per-version base-OS dispatch as this
# project's .NET '-bookworm-slim' fix for glibc-sensitive combos.
#
# Only the RUNTIME stage's OS suffix is pinned explicitly -- verified live
# that the maven BUILDER image's own OS-suffixed tags ('-jammy'/'-noble') are
# only inconsistently published (e.g. '3-eclipse-temurin-21-jammy' and
# '...-22-jammy' exist, but the otherwise-identical '...-8-jammy',
# '...-11-jammy', '...-17-jammy', '...-25-jammy' do NOT -- confirmed 404 for
# all four). The bare untagged 'maven:3-eclipse-temurin-{jdk}' tag exists for
# every version including 23/24 and already resolves to that version's
# correct current default OS, so the builder stage is deliberately left
# unsuffixed to avoid breaking the versions that have no matching tagged
# variant at all.
_NOBLE_JDKS = frozenset({"23", "24"})


def _jdk_os_suffix(jdk_ver: str) -> str:
    return "-noble" if jdk_ver in _NOBLE_JDKS else "-jammy"


def make_dockerfile(jdk_ver: str, fw_name: str) -> str:
    jar_name = _JAR_FILENAME[fw_name]
    os_suffix = _jdk_os_suffix(jdk_ver)
    return (
        f"FROM maven:3-eclipse-temurin-{jdk_ver} AS builder\n"
        "WORKDIR /build\n"
        "COPY pom.xml .\n"
        "COPY src ./src\n"
        "RUN mvn -B -q -DskipTests package\n"
        "\n"
        f"FROM eclipse-temurin:{jdk_ver}-jre{os_suffix}\n"
        "WORKDIR /app\n"
        f"COPY --from=builder /build/target/{jar_name} ./app.jar\n"
        "EXPOSE 8000\n"
        'CMD ["java", "-jar", "app.jar"]\n'
    )


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write pom.xml / src / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required Maven
    coordinate cannot be resolved on Maven Central.
    """
    out = images_base / "java" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    fw_group, fw_artifact = _framework_anchor(fw_name, fw_major)
    fw_resolved = _resolve(fw_group, fw_artifact, fw_major)
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on Maven Central", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    lib_resolved = "built-in"
    lib_coord = _lib_coord(lib_name, lib_ver)
    if lib_coord and lib_ver != "builtin":
        lib_resolved = _resolve(lib_coord[0], lib_coord[1], lib_ver)
        if lib_resolved is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on Maven Central", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    src_dir = out / "src" / "main" / "java" / "app"
    res_dir = out / "src" / "main" / "resources"
    src_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    (src_dir / "Main.java").write_text(
        make_main_java(fw_name, fw_major, lib_name, lib_ver), encoding="utf-8"
    )
    (res_dir / "versions.properties").write_text(
        _versions_properties(fw_resolved, lib_resolved), encoding="utf-8"
    )
    port_config = _PORT_CONFIG[fw_name]
    if port_config:
        (res_dir / "application.properties").write_text(port_config, encoding="utf-8")

    pom_kwargs = {}
    if fw_name == "Micronaut":
        pom_kwargs["parent_group"] = fw_group
        pom_kwargs["fw_major"] = fw_major
    if fw_name == "Quarkus":
        pom_kwargs["fw_major"] = fw_major
    if fw_name == "Vert.x":
        pom_kwargs["fw_major"] = fw_major
    (out / "pom.xml").write_text(
        _POM_TPL[fw_name](lang_ver, fw_resolved, lib_name, lib_resolved, **pom_kwargs),
        encoding="utf-8",
    )
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name), encoding="utf-8"
    )
    return True
