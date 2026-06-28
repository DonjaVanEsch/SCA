"""
Go-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_go").

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
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "go"
REGISTRY_FILE = "registry go.json"


def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


_GO_MODULES_MIN   = (1, 11)   # go.mod support introduced
_GO_BUILDINFO_MIN = (1, 12)   # debug.ReadBuildInfo introduced


# ── Framework helpers ─────────────────────────────────────────────────────────

# fw_major values released before Go modules (no go.mod in the source repo)
_INCOMPATIBLE_FW: frozenset = frozenset({
    ("Gin",         "1.0"), ("Gin",         "1.1"), ("Gin",        "1.2"), ("Gin",        "1.3"),
    ("Gorilla",     "1.1"), ("Gorilla",     "1.2"), ("Gorilla",    "1.3"), ("Gorilla",    "1.4"),
    ("Gorilla",     "1.5"), ("Gorilla",     "1.6"),
    ("Beego",       "1.0"), ("Beego",       "1.6"), ("Beego",      "1.10"),
    ("Iris",        "10.0"), ("Iris",       "10.6"), ("Iris",      "11.1"),
    ("httprouter",  "1.0"), ("httprouter",  "1.1"),
})


def _fw_module(fw_name: str, fw_major: str):
    """Go module root path used in the 'require' line of go.mod."""
    if fw_name == "net/http":
        return None
    ver = _parse(fw_major)
    if fw_name == "Gin":
        return "github.com/gin-gonic/gin"
    if fw_name == "Echo":
        return f"github.com/labstack/echo/v{ver[0]}"
    if fw_name == "Fiber":
        return f"github.com/gofiber/fiber/v{ver[0]}"
    if fw_name == "Chi":
        return "github.com/go-chi/chi/v5"
    if fw_name == "Gorilla":
        return "github.com/gorilla/mux"
    if fw_name == "Beego":
        if ver[0] >= 2:
            return "github.com/beego/beego/v2"
        if ver >= (1, 12):
            return "github.com/beego/beego"
        return "github.com/astaxie/beego"
    if fw_name == "Iris":
        return "github.com/kataras/iris/v12" if ver[0] >= 12 else "github.com/kataras/iris"
    if fw_name == "httprouter":
        return "github.com/julienschmidt/httprouter"
    return None


def _fw_import(fw_name: str, fw_major: str):
    """Go import path used inside main.go (differs from module root for Beego v2)."""
    if fw_name == "net/http":
        return None
    ver = _parse(fw_major)
    if fw_name == "Beego" and ver[0] >= 2:
        return "github.com/beego/beego/v2/server/web"
    return _fw_module(fw_name, fw_major)


# ── Library metadata ──────────────────────────────────────────────────────────

# blank_import: a concrete sub-package to blank-import so the module appears in
# debug/buildinfo even though the app doesn't call the library directly.
LIB_META: dict = {
    "xcrypto":         {"module": "golang.org/x/crypto",                     "blank": "golang.org/x/crypto/sha3",                 "vpath": "golang.org/x/crypto",                     "cgo": False},
    "circl":           {"module": "github.com/cloudflare/circl",              "blank": "github.com/cloudflare/circl/sign/ed25519",  "vpath": "github.com/cloudflare/circl",             "cgo": False},
    "liboqs-go":       {"module": "github.com/open-quantum-safe/liboqs-go",   "blank": "github.com/open-quantum-safe/liboqs-go/oqs", "vpath": "github.com/open-quantum-safe/liboqs-go", "cgo": True},
    "mlkem768":        {"module": "filippo.io/mlkem768",                       "blank": "filippo.io/mlkem768",                       "vpath": "filippo.io/mlkem768",                     "cgo": False},
    "crypto":          {"module": None, "blank": "crypto/sha256",    "vpath": None, "cgo": False},
    "crypto/mlkem":    {"module": None, "blank": "crypto/mlkem",     "vpath": None, "cgo": False},
    "crypto/md5":      {"module": None, "blank": "crypto/md5",       "vpath": None, "cgo": False},
    "crypto/sha1":     {"module": None, "blank": "crypto/sha1",      "vpath": None, "cgo": False},
    "crypto/des":      {"module": None, "blank": "crypto/des",       "vpath": None, "cgo": False},
    "crypto/rc4":      {"module": None, "blank": "crypto/rc4",       "vpath": None, "cgo": False},
    "crypto/elliptic": {"module": None, "blank": "crypto/elliptic",  "vpath": None, "cgo": False},
}

_TINK_V1_MOD   = "github.com/google/tink/go"
_TINK_V1_BLANK = "github.com/google/tink/go/core/registry"
_TINK_V2_MOD   = "github.com/tink-crypto/tink-go/v2"
_TINK_V2_BLANK = "github.com/tink-crypto/tink-go/v2/aead"


def _tink_parts(lib_ver: str):
    if _parse(lib_ver)[0] >= 2:
        return _TINK_V2_MOD, _TINK_V2_BLANK
    return _TINK_V1_MOD, _TINK_V1_BLANK


def _lib_module(lib_name: str, lib_ver: str):
    if lib_name == "tink-go":
        return _tink_parts(lib_ver)[0]
    return LIB_META.get(lib_name, {}).get("module")


def _lib_blank(lib_name: str, lib_ver: str):
    if lib_name == "tink-go":
        return _tink_parts(lib_ver)[1]
    return LIB_META.get(lib_name, {}).get("blank")


def _lib_vpath(lib_name: str, lib_ver: str):
    if lib_name == "tink-go":
        return _tink_parts(lib_ver)[0]
    return LIB_META.get(lib_name, {}).get("vpath")


def _lib_cgo(lib_name: str) -> bool:
    return LIB_META.get(lib_name, {}).get("cgo", False)


# ── Go source helpers ─────────────────────────────────────────────────────────

def _mod_fn(has_bi: bool) -> str:
    """Returns the modVersion() helper function for the generated Go source."""
    if not has_bi:
        return 'func modVersion(_ string) string { return "unknown" }'
    return (
        "func modVersion(path string) string {\n"
        "\tinfo, ok := debug.ReadBuildInfo()\n"
        "\tif !ok {\n"
        '\t\treturn "unknown"\n'
        "\t}\n"
        "\tfor _, d := range info.Deps {\n"
        "\t\tif d.Path == path {\n"
        "\t\t\tif d.Replace != nil {\n"
        "\t\t\t\treturn d.Replace.Version\n"
        "\t\t\t}\n"
        "\t\t\treturn d.Version\n"
        "\t\t}\n"
        "\t}\n"
        '\treturn "unknown"\n'
        "}"
    )


def _lib_import_line(lib_name: str, lib_ver: str) -> str:
    blank = _lib_blank(lib_name, lib_ver)
    return f'\t_ "{blank}"' if blank else ""


def _lib_ver_expr(lib_name: str, lib_ver: str) -> str:
    vp = _lib_vpath(lib_name, lib_ver)
    return f'modVersion("{vp}")' if vp else '"built-in"'


def _fw_ver_expr(fw_name: str, fw_major: str) -> str:
    mod = _fw_module(fw_name, fw_major)
    return f'modVersion("{mod}")' if mod else '"built-in"'


# ── App templates ─────────────────────────────────────────────────────────────
# Templates use plain string substitution via _sub() to avoid f-string issues
# with Go's own { } braces. Tokens: __BI_IMP__, __LIB_IMP__, __MOD_FN__,
# __FW_NAME__, __FW_VER__, __LIB_NAME__, __LIB_VER__.

def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


_NETHTTP_TPL = """\
package main

import (
\t"encoding/json"
\t"net/http"
\t"runtime"__BI_IMP____LIB_IMP__
)

__MOD_FN__

func main() {
\thttp.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
\t})
\thttp.HandleFunc("/version", func(w http.ResponseWriter, r *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]interface{}{
\t\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": map[string]string{"name": "net/http", "version": "built-in"},
\t\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\thttp.ListenAndServe(":8000", nil)
}
"""

_GIN_TPL = """\
package main

import (
\t"net/http"
\t"runtime"__BI_IMP__
\t"github.com/gin-gonic/gin"__LIB_IMP__
)

__MOD_FN__

func main() {
\tr := gin.Default()
\tr.GET("/", func(c *gin.Context) {
\t\tc.JSON(http.StatusOK, gin.H{"message": "Hello World"})
\t})
\tr.GET("/version", func(c *gin.Context) {
\t\tc.JSON(http.StatusOK, gin.H{
\t\t\t"language":  gin.H{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": gin.H{"name": "Gin", "version": __FW_VER__},
\t\t\t"library":   gin.H{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\tr.Run(":8000")
}
"""

_ECHO_TPL = """\
package main

import (
\t"net/http"
\t"runtime"__BI_IMP__
\t"__FW_IMPORT__"__LIB_IMP__
)

__MOD_FN__

func main() {
\te := echo.New()
\te.GET("/", func(c echo.Context) error {
\t\treturn c.JSON(http.StatusOK, map[string]string{"message": "Hello World"})
\t})
\te.GET("/version", func(c echo.Context) error {
\t\treturn c.JSON(http.StatusOK, map[string]interface{}{
\t\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": map[string]string{"name": "Echo", "version": __FW_VER__},
\t\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\te.Start(":8000")
}
"""

_FIBER_TPL = """\
package main

import (
\t"runtime"__BI_IMP__
\t"__FW_IMPORT__"__LIB_IMP__
)

__MOD_FN__

func main() {
\tapp := fiber.New()
\tapp.Get("/", func(c *fiber.Ctx) error {
\t\treturn c.JSON(fiber.Map{"message": "Hello World"})
\t})
\tapp.Get("/version", func(c *fiber.Ctx) error {
\t\treturn c.JSON(fiber.Map{
\t\t\t"language":  fiber.Map{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": fiber.Map{"name": "Fiber", "version": __FW_VER__},
\t\t\t"library":   fiber.Map{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\tapp.Listen(":8000")
}
"""

_CHI_TPL = """\
package main

import (
\t"encoding/json"
\t"net/http"
\t"runtime"__BI_IMP__
\t"github.com/go-chi/chi/v5"__LIB_IMP__
)

__MOD_FN__

func main() {
\tr := chi.NewRouter()
\tr.Get("/", func(w http.ResponseWriter, req *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
\t})
\tr.Get("/version", func(w http.ResponseWriter, req *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]interface{}{
\t\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": map[string]string{"name": "Chi", "version": __FW_VER__},
\t\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\thttp.ListenAndServe(":8000", r)
}
"""

_GORILLA_TPL = """\
package main

import (
\t"encoding/json"
\t"net/http"
\t"runtime"__BI_IMP__
\t"github.com/gorilla/mux"__LIB_IMP__
)

__MOD_FN__

func main() {
\tr := mux.NewRouter()
\tr.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
\t})
\tr.HandleFunc("/version", func(w http.ResponseWriter, r *http.Request) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]interface{}{
\t\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": map[string]string{"name": "Gorilla", "version": __FW_VER__},
\t\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\thttp.ListenAndServe(":8000", r)
}
"""

_BEEGO_TPL = """\
package main

import (
\t"runtime"__BI_IMP__
\tbeego "__FW_IMPORT__"__LIB_IMP__
)

__MOD_FN__

type MainController struct {
\tbeego.Controller
}

func (c *MainController) Get() {
\tc.Data["json"] = map[string]string{"message": "Hello World"}
\tc.ServeJSON()
}

type VersionController struct {
\tbeego.Controller
}

func (c *VersionController) Get() {
\tc.Data["json"] = map[string]interface{}{
\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t"framework": map[string]string{"name": "Beego", "version": __FW_VER__},
\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t}
\tc.ServeJSON()
}

func main() {
\tbeego.Router("/", &MainController{})
\tbeego.Router("/version", &VersionController{})
\tbeego.Run(":8000")
}
"""

_IRIS_TPL = """\
package main

import (
\t"runtime"__BI_IMP__
\t"__FW_IMPORT__"__LIB_IMP__
)

__MOD_FN__

func main() {
\tapp := iris.New()
\tapp.Get("/", func(ctx iris.Context) {
\t\tctx.JSON(iris.Map{"message": "Hello World"})
\t})
\tapp.Get("/version", func(ctx iris.Context) {
\t\tctx.JSON(iris.Map{
\t\t\t"language":  iris.Map{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": iris.Map{"name": "Iris", "version": __FW_VER__},
\t\t\t"library":   iris.Map{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\tapp.Run(iris.Addr(":8000"))
}
"""

_HTTPROUTER_TPL = """\
package main

import (
\t"encoding/json"
\t"net/http"
\t"runtime"__BI_IMP__
\t"github.com/julienschmidt/httprouter"__LIB_IMP__
)

__MOD_FN__

func main() {
\tr := httprouter.New()
\tr.GET("/", func(w http.ResponseWriter, req *http.Request, _ httprouter.Params) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
\t})
\tr.GET("/version", func(w http.ResponseWriter, req *http.Request, _ httprouter.Params) {
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(map[string]interface{}{
\t\t\t"language":  map[string]string{"name": "Go", "version": runtime.Version()},
\t\t\t"framework": map[string]string{"name": "httprouter", "version": __FW_VER__},
\t\t\t"library":   map[string]string{"name": "__LIB_NAME__", "version": __LIB_VER__},
\t\t})
\t})
\thttp.ListenAndServe(":8000", r)
}
"""


def make_main_go(lang_ver: str, fw_name: str, fw_major: str,
                 lib_name: str, lib_ver: str) -> str:
    has_bi    = _parse(lang_ver) >= _GO_BUILDINFO_MIN
    bi_imp    = '\n\t"runtime/debug"' if has_bi else ""
    lib_imp_l = _lib_import_line(lib_name, lib_ver)
    lib_imp   = f"\n{lib_imp_l}" if lib_imp_l else ""
    fw_imp    = _fw_import(fw_name, fw_major) or ""
    fw_ve     = _fw_ver_expr(fw_name, fw_major)
    lib_ve    = _lib_ver_expr(lib_name, lib_ver)
    mod_fn    = _mod_fn(has_bi)

    common = dict(
        BI_IMP    = bi_imp,
        LIB_IMP   = lib_imp,
        MOD_FN    = mod_fn,
        FW_VER    = fw_ve,
        FW_IMPORT = fw_imp,
        LIB_NAME  = lib_name,
        LIB_VER   = lib_ve,
    )

    tpl = {
        "net/http":   _NETHTTP_TPL,
        "Gin":        _GIN_TPL,
        "Echo":       _ECHO_TPL,
        "Fiber":      _FIBER_TPL,
        "Chi":        _CHI_TPL,
        "Gorilla":    _GORILLA_TPL,
        "Beego":      _BEEGO_TPL,
        "Iris":       _IRIS_TPL,
        "httprouter": _HTTPROUTER_TPL,
    }[fw_name]

    return _sub(tpl, **common)


# ── go.mod generation ─────────────────────────────────────────────────────────

def make_go_mod(lang_ver: str, fw_name: str, fw_resolved: str,
                lib_name: str, lib_resolved: str) -> str:
    lines = [f"module app\n\ngo {lang_ver}\n"]
    reqs  = []
    if fw_resolved:
        reqs.append(f"\t{fw_resolved}")
    if lib_resolved:
        reqs.append(f"\t{lib_resolved}")
    if reqs:
        lines.append("require (\n" + "\n".join(reqs) + "\n)\n")
    return "\n".join(lines)


# ── Dockerfile generation ─────────────────────────────────────────────────────

def make_dockerfile(lang_ver: str, lib_name: str, lib_ver: str) -> str:
    uses_modules = _parse(lang_ver) >= _GO_MODULES_MIN
    cgo          = _lib_cgo(lib_name)

    if not uses_modules:
        return _dockerfile_gopath(lang_ver)
    if cgo:
        return _dockerfile_liboqs(lang_ver, lib_ver)
    return _dockerfile_standard(lang_ver)


def _dockerfile_standard(lang_ver: str) -> str:
    return (
        f"FROM golang:{lang_ver} AS builder\n"
        "WORKDIR /build\n"
        "ENV CGO_ENABLED=0 GONOSUMDB=* GOTOOLCHAIN=local\n"
        "COPY go.mod main.go ./\n"
        "RUN go mod tidy && go build -o app main.go\n"
        "\n"
        "FROM scratch\n"
        "COPY --from=builder /build/app /app\n"
        "EXPOSE 8000\n"
        'CMD ["/app"]\n'
    )


def _dockerfile_gopath(lang_ver: str) -> str:
    """Dockerfile for Go < 1.11 using GOPATH mode (no version pinning)."""
    return (
        f"FROM golang:{lang_ver}\n"
        "ENV GOPATH=/go CGO_ENABLED=0\n"
        "WORKDIR /go/src/myapp\n"
        "COPY main.go .\n"
        "RUN go get ./... 2>/dev/null || true && go build -o /app .\n"
        "EXPOSE 8000\n"
        'CMD ["/app"]\n'
    )


# liboqs C 0.1.0/0.2.0/0.3.0 either don't exist as tags or predate -DOQS_BUILD_ONLY_LIB.
# 0.4.0 is the earliest tag with the modern cmake structure we rely on.
_LIBOQS_C_TAG_MAP: dict = {
    "0.1.0": "0.4.0",
}

# liboqs-go doesn't use v-prefixed tags, so the Go proxy only returns @latest
# (a 2026 pseudo-version that calls OQS_SIG_sign_with_ctx_str, present only in
# liboqs C >= 0.12.0). Each entry maps a registry lib_ver to the pseudo-version
# constructed from that release tag's commit date so the Go wrapper is compatible
# with the C library version being built.
_LIBOQS_GO_PSEUDO_VERSIONS: dict = {
    "0.1":  "v0.0.0-20201128215130-cc1b1d62f52c",  # earliest tag (0.4.0)
    "0.4":  "v0.0.0-20201128215130-cc1b1d62f52c",  # tag 0.4.0
    "0.7":  "v0.0.0-20220105163900-e0f759d70fa5",  # tag 0.7.1 (0.7.2 requires OQS_version added in C 0.7.2)
    "0.8":  "v0.0.0-20230705192921-cf9c63b76ce6",  # tag 0.8.0
    "0.9":  "v0.0.0-20231030220805-55a1c61ca0f4",  # tag 0.9.0
    "0.10": "v0.0.0-20240327192735-f3526b7b43ba",  # tag 0.10.0
}


def _dockerfile_liboqs(lang_ver: str, lib_ver: str) -> str:
    """Multi-stage Dockerfile that builds the liboqs C library then the Go app."""
    liboqs_tag = lib_ver if "." in lib_ver else f"{lib_ver}.0"
    if liboqs_tag.count(".") == 1:
        liboqs_tag += ".0"
    liboqs_tag = _LIBOQS_C_TAG_MAP.get(liboqs_tag, liboqs_tag)

    # liboqs C < 0.8.0 has two problems on modern Debian (bookworm/GCC 12):
    # 1. Uses OpenSSL 1.x APIs (find_package requires 1.1.1, but 3.x is installed)
    # 2. compiler_opts.cmake adds -Werror; GCC 12 has new warnings that break old code
    openssl_line = (
        "       -DOQS_USE_OPENSSL=OFF \\\n"
        "       \"-DCMAKE_C_FLAGS=-w\" \\\n"
        if _parse(liboqs_tag) < (0, 8, 0) else ""
    )

    # The pkg-config file name changed from liboqs.pc to liboqs-go.pc in 0.10.0.
    pc_file = "liboqs-go.pc" if _parse(lib_ver) >= (0, 10) else "liboqs.pc"

    return (
        f"FROM golang:{lang_ver} AS builder\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    cmake ninja-build gcc g++ libssl-dev git pkg-config \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"RUN git clone --depth 1 --branch {liboqs_tag} \\\n"
        "    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \\\n"
        "    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \\\n"
        "       -DCMAKE_BUILD_TYPE=Release \\\n"
        "       -DBUILD_SHARED_LIBS=ON \\\n"
        "       -DOQS_BUILD_ONLY_LIB=ON \\\n"
        f"{openssl_line}"
        "       -GNinja \\\n"
        "    && cmake --build /tmp/liboqs/build \\\n"
        "    && cmake --install /tmp/liboqs/build \\\n"
        "    && rm -rf /tmp/liboqs\n"
        "WORKDIR /build\n"
        "ENV GONOSUMDB=* GOTOOLCHAIN=local PKG_CONFIG_PATH=/usr/local/lib/pkgconfig\n"
        "COPY go.mod main.go ./\n"
        "RUN go mod tidy \\\n"
        "    && LDIR=$(go env GOMODCACHE)/$(go list -m github.com/open-quantum-safe/liboqs-go | tr ' ' '@') \\\n"
        "    && mkdir -p /usr/local/lib/pkgconfig/ \\\n"
        f"    && cp \"$LDIR/.config/{pc_file}\" /usr/local/lib/pkgconfig/ \\\n"
        "    && go build -o app main.go\n"
        "\n"
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    libssl3 \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "COPY --from=builder /usr/local/lib/liboqs* /usr/local/lib/\n"
        "RUN ldconfig\n"
        "COPY --from=builder /build/app /app\n"
        "EXPOSE 8000\n"
        'CMD ["/app"]\n'
    )


# ── Go module proxy version resolution ───────────────────────────────────────

_GO_PROXY_CACHE: dict = {}


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0]))
    except ValueError:
        return (0,)


def _fetch_go_versions(module: str) -> list:
    if module in _GO_PROXY_CACHE:
        return _GO_PROXY_CACHE[module]

    url = f"https://proxy.golang.org/{module}/@v/list"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode()
        versions = sorted(
            [v.strip() for v in text.splitlines() if v.strip()],
            key=_ver_key,
        )
    except (URLError, OSError) as exc:
        print(f"  [WARN] Go proxy list failed for {module}: {exc}", flush=True)
        versions = []

    _GO_PROXY_CACHE[module] = versions
    return versions


def _fetch_latest_go_version(module: str):
    url = f"https://proxy.golang.org/{module}/@latest"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("Version")
    except (URLError, json.JSONDecodeError, OSError) as exc:
        print(f"  [WARN] Go proxy @latest failed for {module}: {exc}", flush=True)
        return None


def _resolve_go(module: str, registry_ver: str):
    """Resolve a registry version like '1.8' to an actual Go module tag like 'v1.8.1'.

    Returns the full version string (possibly with +incompatible), or None when
    nothing can be found on the module proxy.
    """
    versions = _fetch_go_versions(module)

    prefix = "v" + registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    exact = "v" + registry_ver
    if exact in versions:
        return exact

    # Try with explicit .0 patch suffix
    for v in versions:
        if v == f"v{registry_ver}.0" or v == f"v{registry_ver}.0+incompatible":
            return v

    # No matching semver tags → fall back to @latest (pseudo-version, e.g. mlkem768)
    if not versions or all(re.search(r"\d{8}", v) for v in versions):
        return _fetch_latest_go_version(module)

    return None


# ── Pre-fetch ─────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from the Go module proxy for all modules."""
    modules: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        for fv in (fw.get("version") or []):
            if isinstance(fv, dict):
                mod = _fw_module(fw["name"], fv["nr"])
                if mod:
                    modules.add(mod)
    for lib in lang_data.get("cryptography_libs", []):
        name = lib["name"]
        if lib.get("version") == "built-in":
            continue
        for lv in (lib.get("version") or []):
            if isinstance(lv, dict):
                mod = _lib_module(name, lv["nr"])
                if mod:
                    modules.add(mod)

    print("Fetching available versions from Go module proxy ...")
    for mod in sorted(modules):
        vers = _fetch_go_versions(mod)
        print(f"  {mod}: {len(vers)} version(s) found")
    print()


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write main.go / go.mod / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required module
    version cannot be resolved on the Go module proxy.
    """
    safe_fw = fw_name.replace("/", "_")
    out = images_base / "go" / lang_ver / safe_fw / fw_major / lib_name / lib_ver

    uses_modules = _parse(lang_ver) >= _GO_MODULES_MIN

    # Pre-module framework versions can't resolve transitive deps under Go modules
    # (broken module paths, missing go.mod, etc.) — skip them entirely.
    if (fw_name, fw_major) in _INCOMPATIBLE_FW and uses_modules:
        if out.exists():
            shutil.rmtree(out)
        return False

    # ── Resolve framework version ──────────────────────────────────────────
    fw_mod      = _fw_module(fw_name, fw_major)
    fw_resolved = ""  # go.mod require line fragment: "module version"

    if fw_mod and uses_modules:
        tag = _resolve_go(fw_mod, fw_major)
        if tag is None:
            print(f"  [SKIP] {fw_name} {fw_major} not found on Go proxy", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False
        if (fw_name, fw_major) in _INCOMPATIBLE_FW and "+incompatible" not in tag:
            tag += "+incompatible"
        fw_resolved = f"{fw_mod} {tag}"

    # ── Resolve library version ────────────────────────────────────────────
    lib_mod      = _lib_module(lib_name, lib_ver)
    lib_resolved = ""  # go.mod require line fragment

    if lib_mod and uses_modules and lib_ver != "builtin":
        if lib_name == "liboqs-go" and lib_ver in _LIBOQS_GO_PSEUDO_VERSIONS:
            tag = _LIBOQS_GO_PSEUDO_VERSIONS[lib_ver]
        else:
            tag = _resolve_go(lib_mod, lib_ver)
        if tag is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not found on Go proxy", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False
        lib_resolved = f"{lib_mod} {tag}"

    # ── Write files ────────────────────────────────────────────────────────
    out.mkdir(parents=True, exist_ok=True)

    (out / "main.go").write_text(
        make_main_go(lang_ver, fw_name, fw_major, lib_name, lib_ver),
        encoding="utf-8",
    )

    if uses_modules:
        (out / "go.mod").write_text(
            make_go_mod(lang_ver, fw_name, fw_resolved, lib_name, lib_resolved),
            encoding="utf-8",
        )

    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, lib_name, lib_ver),
        encoding="utf-8",
    )

    return True
