"""
.NET (C#)-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_dotnet").

Required exports:
    LANGUAGE_ID   - str
    REGISTRY_FILE - str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool
"""

import gzip
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "dotnet"
REGISTRY_FILE = "registry dotnet.json"


class NuGetLookupError(Exception):
    """Raised when a NuGet metadata fetch fails for a network/rate-limit
    reason -- deliberately distinct from _resolve() returning None for a
    package/version actually checked and confirmed absent. Same bug class
    as Java's MavenLookupError (see lang_java.py): conflating the two used
    to make write_context() delete existing output on a transient failure,
    not just a confirmed-gone package. Callers must not delete existing
    output on this exception."""


# ── NuGet version resolution ──────────────────────────────────────────────────
# NuGet's flatcontainer API (api.nuget.org/v3-flatcontainer/{id}/index.json)
# is the direct analog of Maven's maven-metadata.xml used for Java: it lists
# every version ever published for a package id, authoritative and always
# current (unlike search endpoints, which can lag). Like Maven and unlike npm,
# a PackageReference pins an EXACT version -- no range resolution -- so the
# same "resolved version IS what gets installed" reasoning Java relied on to
# skip runtime version-reading gymnastics applies here too (see
# _versions_cs() below).

_NUGET_VERSIONS: dict = {}

# Accepts plain dotted-numeric stable releases only (1, 1.2, 1.2.3, 1.2.3.4).
# Excludes prerelease/build-metadata suffixed versions (-preview, -rc1, -beta,
# +buildinfo) the same way Java's Maven filter excludes Alpha/Beta/RC/SNAPSHOT
# -- NuGet's own SemVer2 prerelease convention uses a literal hyphen, so a
# plain "no hyphen, no plus" regex is sufficient (no equivalent to Spring
# Boot's always-suffixed ".RELEASE" quirk exists in the NuGet ecosystem).
_STABLE_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    except ValueError:
        return (0,)


def _fetch_nuget_versions(package_id: str) -> list:
    """Raises NuGetLookupError on a network/rate-limit failure -- does NOT
    cache that as "zero versions found" (see NuGetLookupError's docstring)."""
    cache_key = package_id.lower()
    if cache_key in _NUGET_VERSIONS:
        return _NUGET_VERSIONS[cache_key]

    url = f"https://api.nuget.org/v3-flatcontainer/{cache_key}/index.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        raw = data.get("versions", [])
        versions = sorted((v for v in raw if _STABLE_RE.match(v)), key=_ver_key)
    except (URLError, OSError, ValueError) as exc:
        raise NuGetLookupError(f"{package_id}: {exc}") from exc

    _NUGET_VERSIONS[cache_key] = versions
    return versions


def _release_date(package_id: str, version: str) -> str | None:
    """release_date for one already-known version, e.g. for a newly detected
    major. The flatcontainer index used above (_fetch_nuget_versions) has no
    per-version dates -- the Registration API is the one that carries a
    `published` timestamp per catalogEntry, only ever fetched here for the
    one version a newly detected major actually resolved to, not the whole
    history."""
    cache_key = package_id.lower()
    url = f"https://api.nuget.org/v3/registration5-gz-semver2/{cache_key}/index.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # server didn't actually gzip it -- raw JSON as-is
        data = json.loads(raw)
        for page in data.get("items", []):
            items = page.get("items")
            if items is None:
                continue  # paginated sub-page -- skip rather than fetch further
            for entry in items:
                catalog = entry.get("catalogEntry", {})
                if catalog.get("version") == version:
                    published = catalog.get("published")
                    return published[:10] if published else None
    except (URLError, OSError, ValueError, KeyError):
        pass
    return None


def _resolve(package_id: str, registry_ver: str) -> str | None:
    """Resolve a registry version like '8' or '2.5' to the latest matching
    stable release on NuGet (e.g. '8' -> '8.0.28')."""
    versions = _fetch_nuget_versions(package_id)

    prefix = registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in versions:
        return registry_ver

    return None


# ── Framework metadata ────────────────────────────────────────────────────────

_FW_PACKAGE: dict = {
    "Carter":        "Carter",
    "FastEndpoints": "FastEndpoints",
    "NancyFx":       "Nancy",
    "ServiceStack":  "ServiceStack",
}
# "ASP.NET Core" is deliberately absent -- built into the runtime, no NuGet
# package, no independent version (see registry notes).


# ── Crypto library metadata ────────────────────────────────────────────────────
# "touch" mirrors Java's LIB_META convention: a real call into the library so
# it's provably loaded and exercised, not just referenced. None import list
# means no extra `using` needed beyond what's already in the template.

LIB_META: dict = {
    "System.Security.Cryptography": {
        "imports": ["System.Security.Cryptography"],
        "touch": "using (var sha = SHA256.Create()) { sha.ComputeHash(new byte[] { 1, 2, 3 }); }",
    },
    "System.Security.Cryptography.PQC": {
        "imports": ["System", "System.Security.Cryptography"],
        # Defensive, matches Java Tink's try/catch-wrapped touch: ML-KEM is
        # GA in .NET 10 but backed by MLKemOpenSsl on Linux, which needs
        # OpenSSL 3.5+ at the OS level -- not guaranteed present in every
        # base image (see the registry's PLATFORM LIMITATION note). Checking
        # IsSupported and catching PlatformNotSupportedException means the
        # app still starts cleanly either way; only PQC *availability* is
        # affected, never buildability/startup.
        "touch": (
            "try\n"
            "            {\n"
            "                if (MLKem.IsSupported)\n"
            "                {\n"
            "                    using var kem = MLKem.GenerateKey(MLKemAlgorithm.MLKem768);\n"
            "                }\n"
            "            }\n"
            "            catch (PlatformNotSupportedException) { /* exercised, PQC provider unavailable on this host */ }"
        ),
    },
    "BouncyCastle.Cryptography": {
        "imports": ["Org.BouncyCastle.Security"],
        "touch": "new SecureRandom().NextBytes(new byte[16]);",
    },
    "NSec.Cryptography": {
        "imports": ["NSec.Cryptography"],
        "touch": "using (Key.Create(SignatureAlgorithm.Ed25519)) { }",
    },
    "LibOQS.NET": {
        "imports": ["LibOQS.NET"],
        "touch": (
            "if (KemAlgorithm.MlKem512.IsEnabled())\n"
            "            {\n"
            "                using var kem = new KemInstance(KemAlgorithm.MlKem512);\n"
            "            }"
        ),
    },
}

_BUILTIN_LIBS = frozenset({"System.Security.Cryptography", "System.Security.Cryptography.PQC"})

# Portable.BouncyCastle (community port, frozen at 1.9.0) vs
# BouncyCastle.Cryptography (official bcgit package since Nov 2022) -- the
# direct C# analog of Java's bcprov-jdk15on/bcprov-jdk18on split. The
# resolved version string doubles as the bucket hint here too, same
# reasoning as lang_java.py's _lib_coord(): every bucket ("1.9", "2.0",
# "2.5", "2") resolves to a version starting with the bucket name itself.
_BC_LEGACY_BUCKET  = "1.9"
_BC_LEGACY_PACKAGE = "Portable.BouncyCastle"
_BC_PACKAGE        = "BouncyCastle.Cryptography"


def _bc_package(lib_ver_bucket: str) -> str:
    return _BC_LEGACY_PACKAGE if lib_ver_bucket == _BC_LEGACY_BUCKET else _BC_PACKAGE


def _lib_package(lib_name: str, lib_ver_bucket: str) -> str | None:
    if lib_name in _BUILTIN_LIBS:
        return None
    if lib_name == "BouncyCastle.Cryptography":
        return _bc_package(lib_ver_bucket)
    return _FW_PACKAGE.get(lib_name) or {
        "NSec.Cryptography": "NSec.Cryptography",
        "LibOQS.NET": "LibOQS.NET",
    }[lib_name]


# ── Pre-fetch ─────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from NuGet for every package this run needs."""
    package_ids: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        pkg = _FW_PACKAGE.get(fw["name"])
        if pkg:
            package_ids.add(pkg)
    for lib in lang_data.get("cryptography_libs", []):
        if lib.get("version") == "built-in":
            continue
        for lv in lib.get("version", []):
            pkg = _lib_package(lib["name"], lv["nr"])
            if pkg:
                package_ids.add(pkg)
    # Microsoft.AspNetCore.Owin (NancyFx's OWIN-bridge dependency) is
    # resolved dynamically per .NET version, not via a registry bucket --
    # pre-warm it for every included language version up front.
    if any(fw["name"] == "NancyFx" and fw.get("include", True) for fw in lang_data.get("frameworks", [])):
        package_ids.add("Microsoft.AspNetCore.Owin")
        package_ids.add("Nancy.Owin")

    print("Fetching available versions from NuGet ...")
    for pkg in sorted(package_ids):
        try:
            versions = _fetch_nuget_versions(pkg)
            print(f"  {pkg}: {len(versions)} version(s) found")
        except NuGetLookupError as exc:
            print(f"  [WARN] {exc}")
    print()


# ── Docker image repository resolution ─────────────────────────────────────────
# Verified live against the MCR v2 registry API: the SDK/aspnet repo path
# moved from 'dotnet/core/{sdk,aspnet}' to the unified 'dotnet/{sdk,aspnet}'
# starting with the 2.1 line: 2.1/3.1/5.0+ are dual-homed or unified-only,
# but 2.2 and 3.0 were NEVER migrated (confirmed 404 on the new path for
# both) and 1.1 predates the split entirely -- all three stay on the old
# 'dotnet/core/...' repo permanently. 1.1 additionally has no dedicated
# ASP.NET-only runtime image at all ("pre-split era"), so its Dockerfile
# collapses to a single stage using the SDK image as the runtime too.

_OLD_REPO_VERSIONS  = frozenset({"1.1", "2.2", "3.0"})
_NO_ASPNET_SPLIT     = frozenset({"1.1"})


def _sdk_repo(lang_ver: str) -> str:
    return "dotnet/core/sdk" if lang_ver in _OLD_REPO_VERSIONS else "dotnet/sdk"


def _aspnet_repo(lang_ver: str) -> str:
    return "dotnet/core/aspnet" if lang_ver in _OLD_REPO_VERSIONS else "dotnet/aspnet"


def _tfm(lang_ver: str) -> str:
    """TargetFramework moniker: netcoreappX.Y pre-5.0, netX.Y from 5.0 on --
    confirmed against Microsoft Learn's /dotnet/standard/frameworks table."""
    major = lang_ver.split(".")[0]
    if major in ("1", "2", "3"):
        return f"netcoreapp{lang_ver}"
    return f"net{lang_ver}"


_LEGACY_ASPNET_VERSIONS = frozenset({"1.1", "2.1", "2.2", "3.0", "3.1", "5.0"})


def _is_legacy_aspnet(lang_ver: str) -> bool:
    return lang_ver in _LEGACY_ASPNET_VERSIONS


# ── Shared C# helpers (Json.cs / Versions.cs) ──────────────────────────────────
# Hand-rolled JSON building (Esc/Obj), mirroring lang_java.py's Helidon
# esc()/obj() helpers, sidesteps needing any JSON library at all: it avoids
# the System.Text.Json (inbox only from .NET Core 3.0 onward) vs
# Newtonsoft.Json (needed pre-3.0) split that would otherwise force two
# different serialization strategies across this project's 1.1-10.0 range.
#
# Classic braced `namespace App { }` syntax is used everywhere (never the
# C#10 file-scoped `namespace App;` form), because the oldest tracked
# versions (1.1-5.0) use whatever Roslyn compiler ships with THAT era's SDK,
# which predates C#10 -- file-scoped namespaces would fail to compile there.

_JSON_CS = """\
namespace App
{
    internal static class Json
    {
        public static string Esc(string s) =>
            "\\"" + s.Replace("\\\\", "\\\\\\\\").Replace("\\"", "\\\\\\"") + "\\"";

        public static string Obj(params string[] kv)
        {
            var sb = new System.Text.StringBuilder("{");
            for (int i = 0; i < kv.Length; i += 2)
            {
                if (i > 0) sb.Append(",");
                sb.Append(Esc(kv[i])).Append(":").Append(kv[i + 1]);
            }
            return sb.Append("}").ToString();
        }
    }
}
"""


def _versions_cs(fw_resolved: str, lib_resolved: str) -> str:
    return (
        "namespace App\n"
        "{\n"
        "    internal static class Versions\n"
        "    {\n"
        f'        public const string Framework = "{fw_resolved}";\n'
        f'        public const string Library = "{lib_resolved}";\n'
        "    }\n"
        "}\n"
    )


def _lang_expr() -> str:
    return "System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription"


def _version_obj_expr(fw_name: str, lib_name: str) -> str:
    # lib_name is interpolated directly (not left as a __LIB_NAME__ token)
    # since this expression is itself substituted into a template via the
    # VERSION_OBJ token -- a nested token here would never get its own
    # replacement pass, as _sub() only walks each template's tokens once.
    return (
        "Json.Obj(\n"
        f'                "language", Json.Obj("name", Json.Esc(".NET"), "version", Json.Esc({_lang_expr()})),\n'
        f'                "framework", Json.Obj("name", Json.Esc("{fw_name}"), "version", Json.Esc(Versions.Framework)),\n'
        f'                "library", Json.Obj("name", Json.Esc("{lib_name}"), "version", Json.Esc(Versions.Library))\n'
        "            )"
    )


# ── App templates, per framework ──────────────────────────────────────────────
# Tokens: __LIB_IMPORTS__, __LIB_TOUCH__, __VERSION_OBJ__.

_ASPNET_LEGACY_PROGRAM = """\
using Microsoft.AspNetCore.Hosting;

namespace App
{
    public class Program
    {
        public static void Main(string[] args)
        {
            var host = new WebHostBuilder()
                .UseKestrel()
                .UseStartup<Startup>()
                .Build();

            host.Run();
        }
    }
}
"""

_ASPNET_LEGACY_STARTUP = """\
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
__LIB_IMPORTS__

namespace App
{
    public class Startup
    {
        static Startup()
        {
            __LIB_TOUCH__
        }

        public void Configure(IApplicationBuilder app)
        {
            app.Run(async context =>
            {
                string body;
                if (context.Request.Path == "/version")
                {
                    body = __VERSION_OBJ__;
                }
                else
                {
                    body = Json.Obj("message", Json.Esc("Hello World"));
                }
                context.Response.ContentType = "application/json";
                await context.Response.WriteAsync(body);
            });
        }
    }
}
"""

_ASPNET_MODERN_PROGRAM = """\
using App;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
__LIB_IMPORTS__

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

__LIB_TOUCH__

app.MapGet("/", () => Results.Text(Json.Obj("message", Json.Esc("Hello World")), "application/json"));

app.MapGet("/version", () => Results.Text(__VERSION_OBJ__, "application/json"));

app.Run();
"""

_CARTER_PROGRAM = """\
using App;
using Carter;
using Microsoft.AspNetCore.Builder;
__LIB_IMPORTS__

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddCarter();
var app = builder.Build();

__LIB_TOUCH__

app.MapCarter();
app.Run();
"""

_CARTER_MODULE = """\
using Carter;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;

namespace App
{
    public class AppModule : ICarterModule
    {
        public void AddRoutes(IEndpointRouteBuilder app)
        {
            app.MapGet("/", () => Results.Text(Json.Obj("message", Json.Esc("Hello World")), "application/json"));

            app.MapGet("/version", () => Results.Text(__VERSION_OBJ__, "application/json"));
        }
    }
}
"""

_FASTENDPOINTS_PROGRAM = """\
using App;
using FastEndpoints;
using Microsoft.AspNetCore.Builder;
__LIB_IMPORTS__

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddFastEndpoints();
var app = builder.Build();

__LIB_TOUCH__

app.UseFastEndpoints();
app.Run();
"""

_FASTENDPOINTS_ENDPOINTS = """\
using FastEndpoints;
using Microsoft.AspNetCore.Http;
using System.Threading;
using System.Threading.Tasks;

namespace App
{
    public class RootEndpoint : EndpointWithoutRequest<string>
    {
        public override void Configure()
        {
            Get("/");
            AllowAnonymous();
        }

        public override async Task HandleAsync(CancellationToken ct)
        {
            HttpContext.Response.ContentType = "application/json";
            await HttpContext.Response.WriteAsync(Json.Obj("message", Json.Esc("Hello World")), ct);
        }
    }

    public class VersionEndpoint : EndpointWithoutRequest<string>
    {
        public override void Configure()
        {
            Get("/version");
            AllowAnonymous();
        }

        public override async Task HandleAsync(CancellationToken ct)
        {
            string body = __VERSION_OBJ__;
            HttpContext.Response.ContentType = "application/json";
            await HttpContext.Response.WriteAsync(body, ct);
        }
    }
}
"""

# AllowSynchronousIO=true is required, not optional, whenever Nancy is
# hosted via the Owin bridge on Kestrel -- found via a real runtime crash on
# EVERY request ("System.InvalidOperationException: Synchronous operations
# are disallowed. Call WriteAsync or set AllowSynchronousIO to true
# instead."), not anticipated. Root cause: Nancy.Responses.TextResponse (the
# type its `Response res = "some string";` implicit conversion produces)
# writes its body via a synchronous Stream.Write call in its Contents
# delegate -- a design that predates async/await entirely and is baked into
# Nancy's Response model, not something a config tweak on Nancy's side can
# fix. Kestrel disallows synchronous response-stream I/O by default (a
# deadlock/thread-starvation safeguard) since ASP.NET Core 3.0 -- both this
# project's 6.0+ NancyFx bucket and any future one will always hit this the
# same way, since Nancy itself has had no async story since the OWIN era.
# The standard, well-known remedy for hosting legacy sync-writing OWIN
# middleware under Kestrel is to explicitly opt back into synchronous I/O
# via KestrelServerOptions -- there is no way to make TextResponse itself
# write asynchronously without replacing Nancy's own response pipeline.
_NANCY_PROGRAM = """\
using App;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Owin;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Nancy.Owin;
__LIB_IMPORTS__

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(options => options.AllowSynchronousIO = true);
var app = builder.Build();

__LIB_TOUCH__

app.UseOwin(pipeline => pipeline.UseNancy());
app.Run();
"""

_NANCY_MODULE = """\
using Nancy;

namespace App
{
    public class AppModule : NancyModule
    {
        public AppModule()
        {
            Get("/", _ =>
            {
                Response res = Json.Obj("message", Json.Esc("Hello World"));
                res.ContentType = "application/json";
                return res;
            });

            Get("/version", _ =>
            {
                string body = __VERSION_OBJ__;
                Response res = body;
                res.ContentType = "application/json";
                return res;
            });
        }
    }
}
"""

_SERVICESTACK_PROGRAM = """\
using App;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using ServiceStack;
__LIB_IMPORTS__

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

__LIB_TOUCH__

app.MapGet("/", () => Results.Text(Json.Obj("message", Json.Esc("Hello World")), "application/json"));
app.UseServiceStack(new AppHost());
app.Run();
"""

# ServiceStack's own [Route] attribute mechanism structurally cannot express
# the bare site root "/" -- found via a real runtime NotSupportedException
# ("RestPath '/' on Type 'RootRequest' is not Valid"), not anticipated.
# Root-caused by reading ServiceStack's own source (RestPath.cs): a route
# path is split into components with empty entries removed, and IsValid is
# only true once at least one non-empty component was appended to its
# internal hash key -- "/" splits into ZERO components, so IsValid is
# always false for it regardless of any other attribute config. There is no
# Route-attribute workaround for this; "/" has to be handled outside
# ServiceStack's RestPath system entirely. Fixed by registering "/" as a
# plain ASP.NET Core Minimal API endpoint directly on `app` (see
# _SERVICESTACK_PROGRAM above), placed BEFORE `app.UseServiceStack(...)` so
# ordinary endpoint routing matches it first -- ServiceStack itself only
# ever handles "/version" via its [Route]-attributed request DTO below.
_SERVICESTACK_HOST = """\
using Funq;
using ServiceStack;

namespace App
{
    public class AppHost : AppHostBase
    {
        public AppHost() : base("PQC SCA App", typeof(AppHost).Assembly) { }

        public override void Configure(Container container) { }
    }

    [Route("/version")]
    public class VersionRequest : IReturn<object> { }

    public class AppServices : Service
    {
        public object Any(VersionRequest request) =>
            new HttpResult(__VERSION_OBJ__, "application/json");
    }
}
"""


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


# NSec.Cryptography's Key.Create(Algorithm, in KeyCreationParameters =
# default) touch (confirmed unchanged across every tracked release,
# 18.6.0 through 26.4.0, by reading src/Cryptography/Key.cs at each tag on
# GitHub) cannot be compiled at all on .NET Core 1.1 -- found via a real
# failing build. KeyCreationParameters is a `ref struct` (its own source
# carries `public ref struct KeyCreationParameters`), and ref structs are a
# C# 7.2 language feature; the 1.1 SDK's bundled compiler (MSBuild
# 15.3/Roslyn ~2.3, frozen since that image predates the 7.2-era tooling
# updates the 2.1+ SDKs kept receiving) rejects ANY source that references
# the type at all with CS0619 ("Types with embedded references are not
# supported in this version of your compiler"), not just the call site --
# confirmed by testing both the no-arg call (misread as a plain mandatory
# `ref` parameter, CS7036) and an explicit `default(KeyCreationParameters)`
# local (CS0619) directly via `docker build` against the real sdk:1.1 image.
# Passing `ref` explicitly to dodge the missing default only works on the
# 1.1 misreading -- confirmed via a second real `docker build` that it is a
# hard CS1615 error ("Argument 2 may not be passed with the 'ref' keyword")
# on every SDK from 2.1 onward, which correctly understands `in` and forbids
# `ref` at that call site. There is no single call spelling that satisfies
# both compiler eras, so 1.1 gets its own touch: RandomGenerator.Default.
# GenerateBytes(int) is untouched by either issue (plain `byte[]` in/out,
# no Span<T>/ref-struct anywhere in its own public signature) while still
# exercising the real native libsodium binding.
_NSEC_LEGACY_LANG_VERSIONS = frozenset({"1.1"})
_NSEC_LEGACY_TOUCH = "NSec.Cryptography.RandomGenerator.Default.GenerateBytes(16);"


def _lib_touch(lib_name: str, lang_ver: str, default_touch: str) -> str:
    if lib_name == "NSec.Cryptography" and lang_ver in _NSEC_LEGACY_LANG_VERSIONS:
        return _NSEC_LEGACY_TOUCH
    return default_touch


def make_source_files(fw_name: str, fw_major: str, lang_ver: str, lib_name: str) -> dict:
    """Return {relative_path: content} for every C# source file the chosen
    framework needs (excluding the always-present Json.cs/Versions.cs, added
    separately in write_context)."""
    meta = LIB_META[lib_name]
    imports = "\n".join(f"using {imp};" for imp in meta["imports"])
    touch = _lib_touch(lib_name, lang_ver, meta["touch"])

    def sub(tpl: str, fw_display_name: str) -> str:
        return _sub(
            tpl,
            LIB_IMPORTS=imports,
            LIB_TOUCH=touch,
            VERSION_OBJ=_version_obj_expr(fw_display_name, lib_name),
        )

    if fw_name == "ASP.NET Core":
        if _is_legacy_aspnet(lang_ver):
            return {
                "Program.cs": sub(_ASPNET_LEGACY_PROGRAM, "ASP.NET Core"),
                "Startup.cs": sub(_ASPNET_LEGACY_STARTUP, "ASP.NET Core"),
            }
        return {"Program.cs": sub(_ASPNET_MODERN_PROGRAM, "ASP.NET Core")}

    if fw_name == "Carter":
        return {
            "Program.cs": sub(_CARTER_PROGRAM, "Carter"),
            "AppModule.cs": sub(_CARTER_MODULE, "Carter"),
        }

    if fw_name == "FastEndpoints":
        return {
            "Program.cs": sub(_FASTENDPOINTS_PROGRAM, "FastEndpoints"),
            "Endpoints.cs": sub(_FASTENDPOINTS_ENDPOINTS, "FastEndpoints"),
        }

    if fw_name == "NancyFx":
        return {
            "Program.cs": sub(_NANCY_PROGRAM, "NancyFx"),
            "AppModule.cs": sub(_NANCY_MODULE, "NancyFx"),
        }

    if fw_name == "ServiceStack":
        return {
            "Program.cs": sub(_SERVICESTACK_PROGRAM, "ServiceStack"),
            "AppHost.cs": sub(_SERVICESTACK_HOST, "ServiceStack"),
        }

    raise ValueError(f"Unknown framework: {fw_name}")


# Microsoft.NET.Sdk.Web only started implicitly pulling in the ASP.NET Core
# shared framework at 3.0 (the "FrameworkReference" mechanism this project
# otherwise relies on for every ASP.NET-Core-hosted framework). Found via a
# real failing build: 1.1/2.1/2.2 all need EXPLICIT package references or
# the compiler can't even resolve Microsoft.AspNetCore.* namespaces at all
# (CS0234), let alone WebHost/IWebHostBuilder/IApplicationBuilder. The shape
# differs by era, confirmed against Microsoft Learn's metapackage docs:
#   1.1  -- no metapackage existed yet; reference the individual
#           Microsoft.AspNetCore.Hosting (WebHostBuilder itself) +
#           Microsoft.AspNetCore.Server.Kestrel (the UseKestrel() extension)
#           packages directly, versioned like any other NuGet dependency --
#           each pulls in what it transitively needs (Http/Http.Abstractions/
#           etc.) on its own. The higher-level 'Microsoft.AspNetCore'
#           convenience package (bundling WebHost.CreateDefaultBuilder) was
#           tried first and does NOT work here: that static helper wasn't
#           added until 2.0, so Program.cs uses the lower-level manual
#           WebHostBuilder construction instead, which both this era's and
#           every later era's package graph supports uniformly.
#   2.1/2.2 -- Microsoft.AspNetCore.App, a metapackage with "special
#           versioning semantics handled outside of NuGet" -- referenced
#           WITHOUT an explicit Version attribute; the SDK resolves it to
#           match the targeted shared framework automatically.
#   3.0+ -- nothing needed here at all; Sdk="Microsoft.NET.Sdk.Web" already
#           implies the FrameworkReference.
def _aspnetcore_package_refs(lang_ver: str) -> list:
    if lang_ver == "1.1":
        refs = []
        for pkg in ("Microsoft.AspNetCore.Hosting", "Microsoft.AspNetCore.Server.Kestrel"):
            try:
                ver = _resolve(pkg, lang_ver)
            except NuGetLookupError as exc:
                print(f"  [WARN] {exc} -- skipping this package ref", flush=True)
                continue
            if ver:
                refs.append((pkg, ver))
        return refs
    if lang_ver in ("2.1", "2.2"):
        return [("Microsoft.AspNetCore.App", None)]
    return []


# ── app.csproj generation ──────────────────────────────────────────────────────
# LibOQS.NET.Native's nupkg DOES ship the real native binary per RID
# (runtimes/linux-x64/native/liboqs.so, confirmed by extracting the actual
# .nupkg and listing its contents -- earlier research only checked the
# nuspec's dependency groups, not the package payload, and wrongly
# concluded this was unconfirmed rather than actually broken). The real bug,
# found via a live DllNotFoundException at runtime ("Unable to load shared
# library 'oqs'"): a framework-dependent `dotnet publish` WITHOUT an
# explicit RuntimeIdentifier never copies `runtimes/{RID}/native/*` assets
# into the publish output at all -- this is standard, documented .NET
# publish behavior (RID-specific assets only get copied for a "framework-
# dependent, RID-specific" or self-contained publish), not specific to this
# package. Fixed by setting <RuntimeIdentifier>linux-x64</RuntimeIdentifier>
# + <SelfContained>false</SelfContained> in the csproj whenever LibOQS.NET
# is selected -- linux-x64 matches this project's amd64-only target (same
# assumption already made for Java's Conscrypt ARM64 gap). `dotnet publish`
# picks up RuntimeIdentifier from the project file automatically, no
# Dockerfile/CLI flag change needed, and `-o /app` still places output
# directly in /app regardless of RID.
_LINUX_RID = "linux-x64"


def make_csproj(lang_ver: str, fw_name: str, fw_resolved: str,
                lib_name: str, lib_ver_bucket: str, lib_resolved: str) -> str:
    tfm = _tfm(lang_ver)
    packages: list = []

    if fw_name == "ASP.NET Core":
        packages.extend(_aspnetcore_package_refs(lang_ver))

    fw_pkg = _FW_PACKAGE.get(fw_name)
    if fw_pkg:
        packages.append((fw_pkg, fw_resolved))
    if fw_name == "NancyFx":
        packages.append(("Nancy.Owin", fw_resolved))
        try:
            owin_ver = _resolve("Microsoft.AspNetCore.Owin", lang_ver)
        except NuGetLookupError as exc:
            print(f"  [WARN] {exc} -- skipping this package ref", flush=True)
            owin_ver = None
        if owin_ver:
            packages.append(("Microsoft.AspNetCore.Owin", owin_ver))

    lib_pkg = _lib_package(lib_name, lib_ver_bucket)
    if lib_pkg:
        packages.append((lib_pkg, lib_resolved))

    item_group = ""
    if packages:
        refs = "\n".join(
            f'    <PackageReference Include="{pid}" />' if ver is None else
            f'    <PackageReference Include="{pid}" Version="{ver}" />'
            for pid, ver in packages
        )
        item_group = f"\n  <ItemGroup>\n{refs}\n  </ItemGroup>\n"

    rid_props = ""
    if lib_name == "LibOQS.NET":
        rid_props = (
            f"    <RuntimeIdentifier>{_LINUX_RID}</RuntimeIdentifier>\n"
            "    <SelfContained>false</SelfContained>\n"
        )

    return (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        "\n"
        "  <PropertyGroup>\n"
        f"    <TargetFramework>{tfm}</TargetFramework>\n"
        "    <AssemblyName>app</AssemblyName>\n"
        f"{rid_props}"
        "  </PropertyGroup>\n"
        f"{item_group}"
        "\n"
        "</Project>\n"
    )


# ── Dockerfile generation ─────────────────────────────────────────────────────
# Multi-stage: mcr.microsoft.com/dotnet/sdk:{version} (or the old
# dotnet/core/sdk repo for 1.1/2.2/3.0) builds and publishes, then a slim
# dotnet/aspnet runtime image runs the published output. No apt-get step
# anywhere in this project's .NET Dockerfiles -- see the registry's
# '_comment_no_apt_needed' note for why none of the 5 tracked crypto
# libraries need one.
#
# An explicit 'dotnet restore' before 'dotnet publish' is required, not
# cosmetic -- found via a real failing build: the .NET Core 1.1 SDK (1.1.14)
# does NOT implicitly restore during 'publish' the way every SDK from
# roughly 2.x onward does, and fails with "Assets file
# '/src/obj/project.assets.json' not found" followed by a cascade of
# unrelated-looking CS0518/CS0246 errors (even 'System.String' itself
# reported as undefined) because NO reference assemblies get resolved at
# all without a restore having run first. Restoring explicitly is harmless
# and a no-op time-wise on newer SDKs that already restore automatically,
# so it's applied unconditionally rather than only for old versions.
#
# LibOQS.NET's prebuilt liboqs.so needs glibc >= 2.34 -- found via a real
# runtime DllNotFoundException ("Unable to load shared library 'oqs'") that
# survived even after fixing the RID/publish issue above: `ldd` on the
# copied liboqs.so inside the container showed
# "GLIBC_2.34' not found (required by /app/liboqs.so)". .NET 6/7's default
# aspnet/sdk images are Debian bullseye-based (glibc 2.31) -- too old.
# .NET 8/9 (bookworm, glibc 2.36+) and 10 (Ubuntu noble, glibc 2.39) are
# unaffected. Fixed by using the '-bookworm-slim' tag suffix (confirmed live
# against the MCR v2 tag-list API to exist for BOTH sdk and aspnet, both
# 6.0 and 7.0) instead of the plain version tag, ONLY for the LibOQS.NET
# combo on 6.0/7.0 -- every other framework/library on 6.0/7.0 keeps the
# default bullseye-based tag unchanged.
_GLIBC_234_NEEDED_LIBS = frozenset({"LibOQS.NET"})
_BULLSEYE_DEFAULT_VERSIONS = frozenset({"6.0", "7.0"})


def _image_tag_suffix(lang_ver: str, lib_name: str) -> str:
    if lib_name in _GLIBC_234_NEEDED_LIBS and lang_ver in _BULLSEYE_DEFAULT_VERSIONS:
        return "-bookworm-slim"
    return ""


def make_dockerfile(lang_ver: str, lib_name: str) -> str:
    sdk_repo = _sdk_repo(lang_ver)
    suffix = _image_tag_suffix(lang_ver, lib_name)

    if lang_ver in _NO_ASPNET_SPLIT:
        return (
            f"FROM mcr.microsoft.com/{sdk_repo}:{lang_ver}{suffix}\n"
            "WORKDIR /src\n"
            "COPY . .\n"
            "RUN dotnet restore\n"
            "RUN dotnet publish -c Release -o /app\n"
            "WORKDIR /app\n"
            "EXPOSE 8000\n"
            "ENV ASPNETCORE_URLS=http://+:8000\n"
            'CMD ["dotnet", "app.dll"]\n'
        )

    aspnet_repo = _aspnet_repo(lang_ver)
    return (
        f"FROM mcr.microsoft.com/{sdk_repo}:{lang_ver}{suffix} AS builder\n"
        "WORKDIR /src\n"
        "COPY . .\n"
        "RUN dotnet restore\n"
        "RUN dotnet publish -c Release -o /app\n"
        "\n"
        f"FROM mcr.microsoft.com/{aspnet_repo}:{lang_ver}{suffix}\n"
        "WORKDIR /app\n"
        "COPY --from=builder /app .\n"
        "EXPOSE 8000\n"
        "ENV ASPNETCORE_URLS=http://+:8000\n"
        'CMD ["dotnet", "app.dll"]\n'
    )


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write app.csproj / *.cs / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required NuGet
    package version is confirmed absent. Returns False WITHOUT touching any
    existing directory when the lookup itself failed (network/rate-limit) --
    see NuGetLookupError.
    """
    out = images_base / "dotnet" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    if fw_name == "ASP.NET Core":
        fw_resolved = lang_ver
    else:
        fw_pkg = _FW_PACKAGE[fw_name]
        try:
            fw_resolved = _resolve(fw_pkg, fw_major)
        except NuGetLookupError as exc:
            print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
            return False
        if fw_resolved is None:
            print(f"  [SKIP] {fw_name} {fw_major} not resolvable on NuGet", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    if lib_name in _BUILTIN_LIBS:
        lib_resolved = "built-in"
    else:
        lib_pkg = _lib_package(lib_name, lib_ver)
        try:
            lib_resolved = _resolve(lib_pkg, lib_ver)
        except NuGetLookupError as exc:
            print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
            return False
        if lib_resolved is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on NuGet", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    out.mkdir(parents=True, exist_ok=True)

    (out / "Json.cs").write_text(_JSON_CS, encoding="utf-8")
    (out / "Versions.cs").write_text(_versions_cs(fw_resolved, lib_resolved), encoding="utf-8")

    for filename, content in make_source_files(fw_name, fw_major, lang_ver, lib_name).items():
        (out / filename).write_text(content, encoding="utf-8")

    (out / "app.csproj").write_text(
        make_csproj(lang_ver, fw_name, fw_resolved, lib_name, lib_ver, lib_resolved),
        encoding="utf-8",
    )
    (out / "Dockerfile").write_text(make_dockerfile(lang_ver, lib_name), encoding="utf-8")
    return True
