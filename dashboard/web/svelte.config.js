import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		// Builds plain static files into web/build; FastAPI serves them.
		adapter: adapter({ fallback: 'index.html' })
	}
};

export default config;
