import { sveltekit } from '@sveltejs/kit/vite';
import adapter from '@sveltejs/adapter-static';
import { defineConfig } from 'vite';

export default defineConfig({
    plugins: [
        sveltekit({
            adapter: adapter({ fallback: 'index.html' })
        })
    ],
    server: {
        proxy: {
            '/api': 'http://localhost:8000'
        }
    }
});