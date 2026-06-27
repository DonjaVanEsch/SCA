package main

import (
	"net/http"
	"runtime"
	"github.com/gin-gonic/gin"
	_ "crypto/sha1"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	r := gin.Default()
	r.GET("/", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "Hello World"})
	})
	r.GET("/version", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{
			"language":  gin.H{"name": "Go", "version": runtime.Version()},
			"framework": gin.H{"name": "Gin", "version": modVersion("github.com/gin-gonic/gin")},
			"library":   gin.H{"name": "crypto/sha1", "version": "built-in"},
		})
	})
	r.Run(":8000")
}
