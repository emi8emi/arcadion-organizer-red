import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		// In dev, anything under /api is forwarded to FastAPI.
		// This keeps login cookies on one address (localhost:5173).
		proxy: {
			'/api': 'http://localhost:8000'
		}
	}
});
