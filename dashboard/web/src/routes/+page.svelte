<script>
	import { onMount } from 'svelte';

	let user = null;
	let events = [];
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
		if (me.ok) {
			user = await me.json();
			const res = await fetch('/api/events');
			if (res.ok) events = await res.json();
		}
		loading = false;
	});

	async function logout() {
		await fetch('/api/auth/logout', { method: 'POST' });
		user = null;
		events = [];
	}
</script>

<main>
	<h1>Event dashboard</h1>

	{#if loading}
		<p>Loading...</p>
	{:else if !user}
		{#if error}<p class="error">{error}</p>{/if}
		<a class="button" href="/api/auth/login">Login with Discord</a>
	{:else}
		<p>
			Logged in as <strong>{user.name}</strong>
			<button on:click={logout}>Logout</button>
		</p>

		<h2>Events</h2>
		{#if events.length === 0}
			<p>No events yet.</p>
		{:else}
			<table>
				<thead>
					<tr><th>Name</th><th>Starts</th><th>Max players</th><th>Status</th></tr>
				</thead>
				<tbody>
					{#each events as e (e.id)}
						<tr>
							<td>{e.name}</td>
							<td>{new Date(e.starts_at).toLocaleString()}</td>
							<td>{e.max_players}</td>
							<td>{e.status}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}
</main>

<style>
	:global(body) {
		font-family: system-ui, sans-serif;
		background: #1e1f22;
		color: #e3e5e8;
		margin: 0;
	}
	main {
		max-width: 720px;
		margin: 0 auto;
		padding: 2rem 1rem;
	}
	.button {
		display: inline-block;
		background: #5865f2;
		color: white;
		padding: 0.6rem 1rem;
		border-radius: 6px;
		text-decoration: none;
	}
	.error {
		color: #f38888;
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	th,
	td {
		text-align: left;
		padding: 0.5rem;
		border-bottom: 1px solid #3a3c41;
	}
	button {
		margin-left: 0.5rem;
	}
</style>
