<script>
	import { onMount } from 'svelte';

	const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
	// One color per fight, so every run of the same fight looks alike.
	const COLORS = [
		{ bg: '#1E2B45', fg: '#8DB8FF' },
		{ bg: '#1D3A2E', fg: '#7FD6A6' },
		{ bg: '#3A2442', fg: '#E0A8F0' },
		{ bg: '#43241F', fg: '#FF9E8A' }
	];

	const pad = (n) => String(n).padStart(2, '0');
	const iso = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;

	const now = new Date();
	const today = iso(now.getFullYear(), now.getMonth(), now.getDate());

	let year = now.getFullYear();
	let month = now.getMonth(); // 0 = January
	let runs = [];
	let loading = true;
	let error = '';

	$: title = new Date(year, month, 1).toLocaleString('en', { month: 'long', year: 'numeric' });
	$: weeks = buildWeeks(year, month);
	$: lanes = assignLanes(runs);
	$: laneCount = Math.max(1, ...Object.values(lanes).map((l) => l + 1));
	$: checkList = runs.flatMap((r) => r.check_days.map((d) => ({ run: r.name, day: d })));

	function buildWeeks(y, m) {
		const lead = new Date(y, m, 1).getDay();
		const total = new Date(y, m + 1, 0).getDate();
		const cells = [
			...Array(lead).fill(null),
			...Array.from({ length: total }, (_, i) => i + 1)
		];
		while (cells.length % 7) cells.push(null);
		const out = [];
		for (let i = 0; i < cells.length; i += 7) out.push(cells.slice(i, i + 7));
		return out;
	}

	// Runs that overlap in time go on separate rows ("lanes").
	function assignLanes(list) {
		const out = {};
		const sorted = [...list].sort((a, b) => a.start_date.localeCompare(b.start_date) || a.id - b.id);
		for (const r of sorted) {
			let lane = 0;
			while (
				list.some((o) => out[o.id] === lane && o.start_date <= r.end_date && r.start_date <= o.end_date)
			)
				lane++;
			out[r.id] = lane;
		}
		return out;
	}

	// Pieces of strip for one week: runs of normal days and runs of skipped days.
	function weekBars(week, list, y, m) {
		const bars = [];
		for (const r of list) {
			let cur = null;
			week.forEach((d, i) => {
				const date = d ? iso(y, m, d) : null;
				if (date && r.start_date <= date && date <= r.end_date) {
					const st = r.skipped_days.includes(date) ? 'skip' : 'on';
					if (cur && cur.st === st && cur.z === i - 1) {
						cur.z = i;
						cur.last = date;
					} else {
						if (cur) bars.push(cur);
						cur = { r, st, a: i, z: i, first: date, last: date };
					}
				} else if (cur) {
					bars.push(cur);
					cur = null;
				}
			});
			if (cur) bars.push(cur);
		}
		return bars.map((b) => ({
			...b,
			check: b.st === 'on' && b.r.check_days.some((d) => d >= b.first && d <= b.last)
		}));
	}

	const isCheckDay = (list, date) => list.some((r) => r.check_days.includes(date));
	const color = (r) => COLORS[r.fight_id % COLORS.length];

	async function load() {
		loading = true;
		error = '';
		try {
			const res = await fetch(`/api/runs?month=${year}-${pad(month + 1)}`);
			if (!res.ok) throw new Error(`server said ${res.status}`);
			runs = await res.json();
		} catch (e) {
			error = `Could not load runs (${e.message}).`;
			runs = [];
		}
		loading = false;
	}

	function shift(delta) {
		const d = new Date(year, month + delta, 1);
		year = d.getFullYear();
		month = d.getMonth();
		load();
	}

	onMount(load);
</script>

<section class="card">
	<div class="top">
		<h2>{title}</h2>
		<div class="nav">
			<button aria-label="Previous month" on:click={() => shift(-1)}>&lsaquo;</button>
			<button aria-label="Next month" on:click={() => shift(1)}>&rsaquo;</button>
		</div>
	</div>

	{#if error}<p class="error">{error}</p>{/if}

	<div class="weekdays">
		{#each WEEKDAYS as w}<div>{w}</div>{/each}
	</div>

	<div class="weeks" class:dim={loading}>
		{#each weeks as week}
			<div class="week" style="grid-template-rows: 28px repeat({laneCount}, 26px)">
				{#each week as d, i}
					{#if d}
						<div
							class="day"
							class:check={isCheckDay(runs, iso(year, month, d))}
							style="grid-row: 1 / span {laneCount + 1}; grid-column: {i + 1}"
						>
							<span class="num" class:today={iso(year, month, d) === today}>{d}</span>
						</div>
					{/if}
				{/each}

				{#each weekBars(week, runs, year, month) as b}
					<div
						class="bar"
						class:skip={b.st === 'skip'}
						class:check={b.check}
						class:cl={b.first === b.r.start_date}
						class:cr={b.last === b.r.end_date}
						style="grid-row: {2 + lanes[b.r.id]}; grid-column: {b.a + 1} / span {b.z - b.a + 1}; --bg: {color(b.r).bg}; --fg: {color(b.r).fg}"
						title="{b.r.name} · {b.r.fight} · {b.r.status}"
					>
						{b.st === 'skip' ? 'Skipped' : b.r.name}
					</div>
				{/each}
			</div>
		{/each}
	</div>

	<p class="legend">
		Each strip is one run across the days it covers (a run with one day is one cell wide). Runs of the
		same fight share a color, and overlapping runs stack. A hatched &ldquo;Skipped&rdquo; strip means
		that run skips that day. Amber outline = needs checking (a draft party waits for approval).
		Days follow the bot&rsquo;s grid timezone.
	</p>
	{#if checkList.length}
		<p class="legend check-line">
			Needs checking: {checkList.map((c) => `${c.day} (${c.run})`).join(', ')}
		</p>
	{/if}
</section>

<section class="card list">
	<h2>Runs in {title}</h2>
	{#if runs.length === 0}
		<p class="legend">No runs this month.</p>
	{:else}
		<div class="scroll">
			<table>
				<thead>
					<tr><th>Name</th><th>Fight</th><th>Dates</th><th>Slots</th><th>Parties</th><th>Status</th></tr>
				</thead>
				<tbody>
					{#each runs as r (r.id)}
						<tr>
							<td>{r.name}</td>
							<td>{r.fight}</td>
							<td>{r.start_date === r.end_date ? r.start_date : `${r.start_date} → ${r.end_date}`}</td>
							<td>{r.slots}</td>
							<td>{r.parties}</td>
							<td>{r.status}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>

<style>
	.card {
		background: #1b1e26;
		border: 1px solid #2a2e39;
		border-radius: 12px;
		padding: 20px;
		margin-bottom: 24px;
	}
	.top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 12px;
	}
	h2 {
		margin: 0;
		font-size: 20px;
	}
	.list h2 {
		margin-bottom: 12px;
	}
	.nav {
		display: flex;
		gap: 8px;
	}
	.nav button {
		min-width: 44px;
		min-height: 44px;
		border-radius: 8px;
		border: 1px solid #2a2e39;
		background: transparent;
		color: inherit;
		cursor: pointer;
		font-size: 18px;
	}
	.weekdays,
	.week {
		display: grid;
		grid-template-columns: repeat(7, 1fr);
		column-gap: 6px;
	}
	.weekdays {
		margin-bottom: 6px;
		font-size: 12px;
		color: #a3a7b3;
		padding: 0 4px;
	}
	.weeks {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.weeks.dim {
		opacity: 0.6;
	}
	.day {
		box-sizing: border-box;
		border: 1px solid #2a2e39;
		background: #12141a;
		border-radius: 8px;
		padding: 6px 8px;
		min-width: 0;
	}
	.day.check {
		border-color: #e8b04a;
	}
	.num {
		font-size: 13px;
		color: #a3a7b3;
	}
	.num.today {
		background: #ece9e4;
		color: #12141a;
		border-radius: 99px;
		padding: 1px 7px;
		font-weight: 700;
	}
	.bar {
		box-sizing: border-box;
		margin: 2px 3px;
		height: 22px;
		line-height: 22px;
		padding: 0 8px;
		font-size: 12px;
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		background: var(--bg);
		color: var(--fg);
	}
	.bar.cl {
		border-top-left-radius: 6px;
		border-bottom-left-radius: 6px;
	}
	.bar.cr {
		border-top-right-radius: 6px;
		border-bottom-right-radius: 6px;
	}
	.bar.check {
		box-shadow: inset 0 0 0 2px #e8b04a;
	}
	.bar.skip {
		background: repeating-linear-gradient(135deg, var(--bg), var(--bg) 5px, #12141a 5px, #12141a 10px);
		color: #a3a7b3;
		border: 1px dashed var(--fg);
		line-height: 20px;
	}
	.legend {
		margin: 16px 0 0;
		font-size: 13px;
		color: #a3a7b3;
	}
	.check-line {
		margin-top: 4px;
		color: #e8b04a;
	}
	.error {
		color: #f38888;
	}
	.scroll {
		overflow-x: auto;
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	th,
	td {
		text-align: left;
		padding: 10px 12px;
		border-top: 1px solid #2a2e39;
		white-space: nowrap;
	}
	th {
		font-size: 13px;
		font-weight: 400;
		color: #a3a7b3;
		border-top: 0;
	}
</style>
