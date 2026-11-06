<script>
	import { onMount } from 'svelte';

	let user = null;
	let loading = true;
	let error = '';

	const errorText = {
		not_in_server: 'You are not a member of the server.',
		not_organizer: 'You need the organizer role to use the dashboard.',
		discord_error: 'Discord had a problem. Try again in a moment.'
	};

	onMount(async () => {
		const code = new URLSearchParams(location.search).get('error');
		if (code) error = errorText[code] ?? 'Login failed.';

		const me = await fetch('/api/auth/me');
		if (me.ok) user = await me.json();
		loading = false;
	});

	async function logout() {
		await fetch('/api/auth/logout', { method: 'POST' });
		user = null;
	}
</script>

{#if loading}
	<p class="center">Loading...</p>
{:else if !user}
	<main class="login">
		<h1>Raid desk</h1>
		{#if error}<p class="error">{error}</p>{/if}
		<a class="button" href="/api/auth/login">Login with Discord</a>
	</main>
{:else}
	<div class="shell">
		<aside>
			<div class="brand">Raid desk</div>
			<nav>
				<a href="/" aria-current="page" class="active">Runs</a>
				<span class="soon" aria-disabled="true">Matchmaking <small>soon</small></span>
				<span class="soon" aria-disabled="true">Participants <small>soon</small></span>
			</nav>
			<div class="who">
				<span>Logged in as <strong>{user.name}</strong></span>
				<button on:click={logout}>Logout</button>
			</div>
		</aside>
		<main class="content">
			<div class="inner"><slot /></div>
		</main>
	</div>
{/if}

<style>
	:global(body) {
		margin: 0;
		background: #12141a;
		color: #ece9e4;
		font-family: 'Bricolage Grotesque', system-ui, sans-serif;
	}
	:global(h1) {
		margin: 0 0 24px;
		font-size: 32px;
		font-weight: 700;
	}
	.center {
		padding: 2rem;
	}
	.login {
		max-width: 420px;
		margin: 0 auto;
		padding: 4rem 1rem;
	}
	.button {
		display: inline-block;
		background: #e8b04a;
		color: #1a1405;
		font-weight: 600;
		padding: 0.7rem 1.2rem;
		border-radius: 8px;
		text-decoration: none;
	}
	.error {
		color: #f38888;
	}
	.shell {
		display: flex;
		min-height: 100vh;
	}
	aside {
		width: 220px;
		flex: none;
		box-sizing: border-box;
		background: #0e1015;
		border-right: 1px solid #2a2e39;
		padding: 24px 16px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.brand {
		font-weight: 700;
		font-size: 20px;
		padding: 0 12px 20px;
	}
	nav {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	nav a,
	.soon {
		padding: 10px 12px;
		border-radius: 8px;
		font-size: 15px;
		text-decoration: none;
		color: #a3a7b3;
	}
	nav a.active {
		color: #e8b04a;
		background: #262219;
		font-weight: 600;
	}
	.soon {
		opacity: 0.7;
	}
	.soon small {
		font-size: 11px;
		margin-left: 6px;
	}
	.who {
		margin-top: auto;
		padding: 12px;
		font-size: 13px;
		color: #a3a7b3;
		display: flex;
		flex-direction: column;
		gap: 8px;
		align-items: flex-start;
	}
	.who button {
		font: inherit;
		min-height: 44px;
		padding: 0 14px;
		border-radius: 8px;
		border: 1px solid #2a2e39;
		background: transparent;
		color: #ece9e4;
		cursor: pointer;
	}
	.content {
		flex: 1;
		min-width: 0;
		padding: 32px;
	}
	.inner {
		max-width: 1100px;
	}
	@media (max-width: 760px) {
		.shell {
			flex-direction: column;
		}
		aside {
			width: auto;
			border-right: 0;
			border-bottom: 1px solid #2a2e39;
		}
		.who {
			margin-top: 0;
		}
		.content {
			padding: 16px;
		}
	}
</style>
