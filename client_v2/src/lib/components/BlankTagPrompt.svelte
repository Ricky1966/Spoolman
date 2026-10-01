<script lang="ts">
	import { onMount } from 'svelte';
	import Button from './Button.svelte';
	import { getJson } from '$lib/api/http';

	/** A blank tag is on the Phoenix reader: ask which spool it belongs to, then hand over to the writer. */
	let {
		uid,
		matchedSpoolId,
		onwrite,
		onclose
	}: {
		uid: string;
		/** Set when the UID is already linked to a spool (a wiped tag): that spool is the only candidate. */
		matchedSpoolId?: number;
		onwrite: (spoolId: number) => void;
		onclose: () => void;
	} = $props();

	type Candidate = { id: number; label: string };
	let candidates = $state<Candidate[]>([]);
	let chosen = $state('');
	let loading = $state(true);
	let error = $state('');
	let dialog: HTMLDialogElement;

	/* eslint-disable @typescript-eslint/no-explicit-any */
	function label(s: any): string {
		const f = s.filament ?? {};
		const parts = [f.vendor?.name, f.name].filter(Boolean).join(' ');
		return `#${s.id} · ${parts || 'senza nome'}${f.color_hex ? ` · #${String(f.color_hex).replace('#', '')}` : ''}`;
	}

	onMount(() => {
		dialog.showModal();
		getJson<any[]>('/spool', { allow_archived: 'false' })
			.then((rows) => {
				const free = rows.filter((s) =>
					matchedSpoolId ? s.id === matchedSpoolId : !s.archived && (s.tags ?? []).length === 0
				);
				candidates = free.map((s) => ({ id: s.id, label: label(s) }));
				chosen = candidates.length === 1 ? String(candidates[0].id) : '';
			})
			.catch((e) => (error = String(e?.body?.detail ?? e?.message ?? e)))
			.finally(() => (loading = false));
	});

	function confirm() {
		dialog.close();
		onwrite(Number(chosen));
	}
	function close() {
		dialog.close();
		onclose();
	}
</script>

<dialog
	bind:this={dialog}
	oncancel={(e) => {
		e.preventDefault();
		close();
	}}
	aria-labelledby="blank-tag-title"
>
	<h2 id="blank-tag-title">Tag vuoto rilevato</h2>
	<p>
		Il lettore Phoenix ha trovato un tag vuoto: <strong>{uid}</strong>. Vuoi scrivere su di esso i dati di una
		bobina?
	</p>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if loading}
		<p role="status">Cerco le bobine senza tag…</p>
	{:else if candidates.length === 0}
		<p>
			Nessuna bobina senza tag. Crea prima la bobina in Spoolman, poi riappoggia il tag sul lettore (o aprila
			e usa «Scrivi tag»).
		</p>
	{:else if candidates.length === 1}
		<p>Bobina proposta: <strong>{candidates[0].label}</strong></p>
	{:else}
		<label
			><span>Più bobine senza tag: scegli quella su cui hai appena incollato il tag</span>
			<select bind:value={chosen}>
				<option value="">Scegli una bobina…</option>
				{#each candidates as c (c.id)}<option value={String(c.id)}>{c.label}</option>{/each}
			</select>
		</label>
	{/if}
	<p>
		Dopo la conferma vedrai l’anteprima dei dati: la scrittura parte solo quando premi «Conferma e scrivi
		tag». Tieni la bobina ferma.
	</p>
	<footer>
		<Button variant="outline" onclick={close}>Ignora</Button>
		<Button onclick={confirm} disabled={loading || !chosen}>Scrivi su questa bobina</Button>
	</footer>
</dialog>

<style>
	dialog {
		color: var(--text);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		width: min(560px, calc(100vw - 32px));
		padding: 24px;
	}
	dialog::backdrop {
		background: #0009;
	}
	h2 {
		margin: 0 0 14px;
		font-size: 20px;
	}
	p {
		font-size: 13px;
		line-height: 1.5;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 5px;
		font-size: 12px;
	}
	select {
		background: var(--surface);
		color: var(--text);
		border: 1px solid var(--border);
		padding: 8px;
		border-radius: 5px;
		width: 100%;
		box-sizing: border-box;
	}
	.error {
		color: var(--danger);
	}
	footer {
		display: flex;
		justify-content: flex-end;
		gap: 10px;
		margin-top: 20px;
		flex-wrap: wrap;
	}
</style>
