package main

import (
	"net/http"
	"runtime"
	"runtime/debug"
	"github.com/gin-gonic/gin"
	_ "crypto/des"
)

func modVersion(path string) string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, d := range info.Deps {
		if d.Path == path {
			if d.Replace != nil {
				return d.Replace.Version
			}
			return d.Version
		}
	}
	return "unknown"
}

func main() {
	r := gin.Default()
	r.GET("/", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "Hello World"})
	})
	r.GET("/version", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{
			"language":  gin.H{"name": "Go", "version": runtime.Version()},
			"framework": gin.H{"name": "Gin", "version": modVersion("github.com/gin-gonic/gin")},
			"library":   gin.H{"name": "crypto/des", "version": "built-in"},
		})
	})
	r.Run(":8000")
}
