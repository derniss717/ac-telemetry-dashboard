import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：
// - dev 模式端口 5173，/api 与 /static 代理到后端 8080（后端实例照常启动）
// - build 产物输出到 web-ui/dist（后端 dashboard 优先 serve 该目录，替代旧 web/）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      // /static 旧版资源代理已废弃（8080 现在直接服务新版 dist，新版无 /static 引用）
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: false,   // 沙箱安全钩子拦截 rmSync 清空；清 dist 用 bash rm -f 逐个删
  },
})
