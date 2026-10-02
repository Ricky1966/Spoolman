<script lang="ts">
	import { onMount } from 'svelte';
	import { scanner } from '$lib/stores/scanner.svelte';
	import Button from './Button.svelte';
	import { getJson, postJson, HttpError } from '$lib/api/http';

	/** Erase a tag of this spool: the tag goes back to blank and its link to the spool is removed. */
	let { id, uid, onclose }: { id: number | string; uid: string; onclose: () => void } = $props();

	type Prepared = { session: string; uid: string; blank: boolean; expires_seconds: number };
	type Outcome = {
		verified: boolean;
		erased?: boolean;
		unlinked?: boolean;
		message?: string;
		backup?: string;
		reader_result?: { status: string };
	};
	let enabled = $state(false);
	let busy = $state(true);
	let error = $state('');
	let reservation = $state('');
	let prepared = $state<Prepared | null>(null);
	let outcome = $state<Outcome | null>(null);
	let consent = $state(false);
	let consumed = $state(false);
	let deadline = $state(0);
	let seconds = $state(0);
	let dialog: HTMLDialogElement;
	const wrongTag = $derived(!!(prepared && prepared.uid !== uid));

	function message(e: unknown) {
		return e instanceof HttpError ? String(e.body?.detail ?? e.message) : String(e);
	}
	onMount(() => {
		const releaseNavigation = scanner.suppress();
		dialog.showModal();
		let active = true;
		getJson<{ enabled: boolean }>('/writer/schema')
			.then(async (schema) => {
				if (!active) return;
				enabled = schema.enabled;
				if (schema.enabled) {
					const opened = await postJson<{ session: string }>('/writer/open', { spool_id: Number(id) });
					reservation = opened.session;
				}
			})
			.catch((e) => {
				if (active) error = message(e);
			})
			.finally(() => {
				if (active) busy = false;
			});
		const timer = setInterval(() => {
			seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
		}, 250);
		return () => {
			active = false;
			releaseNavigation();
			clearInterval(timer);
		};
	});
	async function inspect() {
		busy = true;
		error = '';
		try {
			prepared = await postJson<Prepared>('/writer/prepare-erase', {
				session: reservation,
				spool_id: Number(id)
			});
			deadline = Date.now() + prepared.expires_seconds * 1000;
			seconds = prepared.expires_seconds;
		} catch (e) {
			error = message(e);
		} finally {
			busy = false;
		}
	}
	async function erase() {
		if (!prepared || consumed) return;
		busy = true;
		consumed = true;
		error = '';
		try {
			outcome = await postJson<Outcome>('/writer/commit', { session: prepared.session, replace: true });
		} catch (e) {
			error = message(e) + ' — Non ripetere la cancellazione: controlla prima il tag.';
		} finally {
			busy = false;
		}
	}
	async function close() {
		if (busy) return;
		busy = true;
		try {
			if (reservation) await postJson('/writer/cancel', { session: reservation });
			dialog.close();
			onclose();
		} catch (e) {
			error = message(e);
		} finally {
			busy = false;
		}
	}
</script>

<dialog
	bind:this={dialog}
	oncancel={(e) => {
		e.preventDefault();
		void close();
	}}
	aria-labelledby="erase-tag-title"
>
	<h2 id="erase-tag-title">Cancella tag · Bobina #{id}</h2>
	<p>
		Il tag <strong>{uid}</strong> torna vuoto e viene scollegato da questa bobina, così potrai riusarlo. Tutti i
		dati presenti sul tag vengono cancellati; il demone ne salva prima una copia.
	</p>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if !enabled && !busy}<p>
			La scrittura non è configurata sul server. Configura il collegamento al demone Phoenix prima di
			procedere.
		</p>{/if}
	{#if !prepared}
		{#if reservation}<p role="status">
				Lettore riservato: le scansioni automatiche sono sospese. Appoggia il tag sul lettore e tienilo fermo.
			</p>{/if}
	{:else}
		<p>
			<strong>UID sul lettore: {prepared.uid}</strong> · {prepared.blank ? 'Tag già vuoto' : 'Tag con dati'}
		</p>
		{#if wrongTag}<p class="error" role="alert">
				Sul lettore c’è un tag diverso da {uid}. Chiudi, appoggia il tag giusto e riprova.
			</p>{/if}
		{#if !consumed}
			<label class="consent"
				><input type="checkbox" bind:checked={consent} />Confermo di voler cancellare questo tag e scollegarlo
				dalla bobina.</label
			>
			<p>Conferma entro {seconds} s. Lascia fermo il tag.</p>
		{/if}
		{#if outcome}
			<p role="status">
				{outcome.verified && outcome.erased
					? outcome.unlinked
						? 'Tag cancellato, verificato e scollegato dalla bobina.'
						: (outcome.message ?? 'Tag cancellato e verificato.')
					: (outcome.message ??
						`Cancellazione non confermata: ${outcome.reader_result?.status ?? 'errore'}. Il tag potrebbe essere cancellato solo in parte: ripeti l’operazione.`)}
			</p>
			{#if outcome.backup}<p>Backup: {outcome.backup}</p>{/if}
			<p>Rimuovi il tag prima di chiudere.</p>
		{/if}
	{/if}
	{#if busy}<p role="status">
			{consumed ? 'Cancellazione e verifica in corso… Non muovere il tag.' : 'Comunicazione con il lettore…'}
		</p>{/if}
	<footer>
		<Button variant="outline" onclick={close} disabled={busy}>Chiudi</Button>
		{#if !prepared}<Button onclick={inspect} disabled={busy || !enabled || !reservation}>Leggi tag</Button>
		{:else if !consumed}<Button
				variant="danger"
				onclick={erase}
				disabled={busy || wrongTag || !consent || seconds <= 0}>Cancella tag</Button
			>{/if}
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
	.consent {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 8px;
		font-size: 12px;
	}
	.consent input {
		width: auto;
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
