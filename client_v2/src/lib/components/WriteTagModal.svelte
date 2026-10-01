<script lang="ts">
	import { onMount } from 'svelte';
	import { scanner } from '$lib/stores/scanner.svelte';
	import Button from './Button.svelte';
	import { getJson, postJson, HttpError } from '$lib/api/http';

	let { id, onclose }: { id: number | string; onclose: () => void } = $props();
	type Field = {
		name: string;
		type: string;
		category?: string;
		unit?: string;
		max_length?: number;
		description?: string | string[];
		options?: { value: string; label: string }[];
	};
	type Preview = {
		session: string;
		uid: string;
		blank: boolean;
		expires_seconds: number;
		preview: {
			main: Record<string, unknown>;
			aux: Record<string, unknown>;
			main_bytes: number;
			main_capacity: number;
			aux_bytes: number;
			aux_capacity: number;
		};
	};
	type Outcome = {
		verified: boolean;
		linked: boolean;
		uid?: string;
		message?: string;
		backup?: string;
		reader_result?: { status: string };
	};
	let fields = $state<Record<string, Field[]>>({});
	let values = $state<Record<string, string>>({});
	let enabled = $state(false);
	let busy = $state(true);
	let error = $state('');
	let reservation = $state('');
	let preview = $state<Preview | null>(null);
	let outcome = $state<Outcome | null>(null);
	let replace = $state(false);
	let consumed = $state(false);
	let deadline = $state(0);
	let seconds = $state(0);
	let dialog: HTMLDialogElement;
	let resetConfirmed = $state(false);

	function message(e: unknown) {
		return e instanceof HttpError ? String(e.body?.detail ?? e.message) : String(e);
	}
	onMount(() => {
		const releaseNavigation = scanner.suppress();
		dialog.showModal();
		let active = true;
		Promise.all([
			getJson<{ enabled: boolean; fields: Record<string, Field[]> }>('/writer/schema'),
			getJson<Record<string, Record<string, unknown>>>(`/writer/defaults/${id}`)
		])
			.then(async ([schema, defaults]) => {
				if (!active) return;
				enabled = schema.enabled;
				fields = schema.fields;
				if (schema.enabled) {
					const opened = await postJson<{ session: string }>('/writer/open', { spool_id: Number(id) });
					reservation = opened.session;
				}
				for (const [region, data] of Object.entries(defaults))
					for (const [name, value] of Object.entries(data)) {
						values[`${region}.${name}`] = Array.isArray(value) ? JSON.stringify(value) : String(value);
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
	function payload() {
		const body: Record<string, Record<string, unknown>> = { main: {}, aux: {} };
		for (const [region, rows] of Object.entries(fields))
			for (const f of rows) {
				const text = (values[`${region}.${f.name}`] ?? '').trim();
				if (!text) continue;
				let value: unknown = text;
				if (['number', 'int', 'timestamp'].includes(f.type)) {
					value = Number(text);
					if (!Number.isFinite(value)) throw new Error(`${f.name}: numero non valido`);
				} else if (f.type === 'bool') value = text === 'true';
				else if (['enum_array', 'color_lab'].includes(f.type)) value = JSON.parse(text);
				body[region][f.name] = value;
			}
		return { spool_id: Number(id), session: reservation, ...body };
	}
	async function resetReader() {
		busy = true;
		error = '';
		try {
			await postJson('/writer/reset', { confirm: true });
			const opened = await postJson<{ session: string }>('/writer/open', { spool_id: Number(id) });
			reservation = opened.session;
			resetConfirmed = false;
		} catch (e) {
			error = message(e);
		} finally {
			busy = false;
		}
	}
	async function inspect() {
		busy = true;
		error = '';
		try {
			preview = await postJson<Preview>('/writer/prepare', payload());
			deadline = Date.now() + preview.expires_seconds * 1000;
			seconds = preview.expires_seconds;
		} catch (e) {
			error = message(e);
		} finally {
			busy = false;
		}
	}
	async function write() {
		if (!preview || consumed) return;
		busy = true;
		consumed = true;
		error = '';
		try {
			outcome = await postJson<Outcome>('/writer/commit', { session: preview.session, replace });
		} catch (e) {
			error = message(e) + ' — Non ripetere la scrittura: controlla prima il tag.';
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
	aria-labelledby="write-tag-title"
>
	<header><h2 id="write-tag-title">Scrivi OpenPrintTag · Bobina #{id}</h2></header>
	<p>
		Lettore Phoenix USB · ICODE SLIX2 · 316 byte. Attendi che il lettore sia riservato, compila i dati,
		appoggia un solo tag e tienilo fermo fino alla conferma.
	</p>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if !enabled && !busy}<p>
			La scrittura non è configurata sul server. Configura il collegamento al demone Phoenix prima di
			procedere.
		</p>{/if}
	{#if !preview}
		<div class="fields">
			{#each Object.entries(fields) as [region, rows] (region)}
				<details open={region === 'main'}>
					<summary>{region === 'main' ? 'Materiale e bobina' : 'Dati di utilizzo'} · campi ufficiali</summary>
					<div class="grid">
						{#each rows.filter((f) => f.category !== 'sla') as f (f.name)}
							<label
								><span>{f.name.replaceAll('_', ' ')} {f.unit ? `(${f.unit})` : ''}</span>
								{#if f.type === 'enum' || f.type === 'bool'}
									<select
										bind:value={values[`${region}.${f.name}`]}
										disabled={busy || f.name === 'material_class' || f.name === 'write_protection'}
									>
										<option value="">Non specificato</option>
										{#if f.type === 'bool'}<option value="true">Sì</option><option value="false">No</option
											>{:else}{#each f.options ?? [] as opt (opt.value)}<option value={opt.value}
													>{opt.label || opt.value}</option
												>{/each}{/if}
									</select>
								{:else}
									<input
										bind:value={values[`${region}.${f.name}`]}
										disabled={busy}
										placeholder={f.type === 'uuid'
											? 'UUID (istanza automatica se vuoto)'
											: f.type === 'timestamp'
												? 'Secondi Unix UTC'
												: f.type === 'enum_array'
													? '["valore", "valore"]'
													: f.type === 'color_lab'
														? '[L, a, b]'
														: f.type === 'color_rgba'
															? '#RRGGBB'
															: 'Facoltativo'}
									/>
								{/if}
								{#if f.type === 'enum_array'}<small>Valori: {f.options?.map((o) => o.value).join(', ')}</small
									>{/if}
							</label>
						{/each}
					</div>
				</details>
			{/each}
		</div>
		<details>
			<summary>Recupera una sessione rimasta aperta</summary>
			<p>
				Usa questa opzione solo dopo un errore o una pagina chiusa. Non ripristina il tag e non conferma una
				scrittura precedente. Rimuovi il tag prima di continuare.
			</p>
			<label class="replace"
				><input type="checkbox" bind:checked={resetConfirmed} />Ho rimosso il tag e voglio chiudere la
				sessione precedente.</label
			>
			<Button variant="outline" onclick={resetReader} disabled={busy || !resetConfirmed || !enabled}
				>Libera il lettore</Button
			>
		</details>
		{#if reservation}<p role="status">
				Lettore riservato: le scansioni automatiche sono sospese. Ora puoi appoggiare il tag.
			</p>{/if}
		<p>
			I campi vuoti vengono omessi. Se i dati superano lo spazio disponibile, la scrittura viene rifiutata
			senza eliminare campi automaticamente. Questa operazione inizializza l’intero tag; non aggiorna soltanto
			il peso residuo.
		</p>
	{:else}
		<p><strong>UID: {preview.uid}</strong> · {preview.blank ? 'Tag vuoto' : 'Tag già scritto'}</p>
		<p>
			Materiale: {String(preview.preview.main.brand_name ?? '')}
			{String(preview.preview.main.material_name ?? '')}
		</p>
		<p>
			Memoria: dati principali {preview.preview.main_bytes}/{preview.preview.main_capacity} byte; utilizzo {preview
				.preview.aux_bytes}/{preview.preview.aux_capacity} byte.
		</p>
		{#if !preview.blank && !consumed}<label class="replace"
				><input type="checkbox" bind:checked={replace} />Autorizzo la sostituzione di tutti i dati del tag,
				anche quelli non presenti nel form. Il demone salva prima un backup.</label
			>{/if}
		{#if !consumed}<p>Conferma entro {seconds} s. Controlla l’UID e lascia fermo il tag.</p>{/if}
		{#if outcome}
			<p role="status">
				{outcome.verified && outcome.linked
					? 'Scrittura verificata e tag associato alla bobina.'
					: (outcome.message ??
						`Scrittura non confermata: ${outcome.reader_result?.status ?? 'errore'}. Nessuna associazione eseguita.`)}
			</p>
			{#if outcome.backup}<p>Backup: {outcome.backup}</p>{/if}
			<p>Rimuovi il tag prima di chiudere.</p>
		{/if}
	{/if}
	{#if busy}<p role="status">
			{consumed ? 'Scrittura e verifica in corso… Non muovere il tag.' : 'Comunicazione con il lettore…'}
		</p>{/if}
	<footer>
		<Button variant="outline" onclick={close} disabled={busy}>Chiudi</Button>
		{#if !preview}<Button onclick={inspect} disabled={busy || !enabled || !reservation}
				>Leggi tag e prepara anteprima</Button
			>
		{:else if !consumed}<Button
				onclick={write}
				disabled={busy || seconds <= 0 || (!preview.blank && !replace)}>Conferma e scrivi tag</Button
			>{/if}
	</footer>
</dialog>

<style>
	dialog {
		color: var(--text);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		width: min(850px, calc(100vw - 32px));
		max-height: 90vh;
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
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
		gap: 12px;
		margin: 16px 0;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 5px;
		font-size: 12px;
	}
	input,
	select {
		background: var(--surface);
		color: var(--text);
		border: 1px solid var(--border);
		padding: 8px;
		border-radius: 5px;
		width: 100%;
		box-sizing: border-box;
	}
	small {
		color: var(--text-muted);
		overflow-wrap: anywhere;
	}
	.fields {
		max-height: 48vh;
		overflow: auto;
		padding: 4px;
	}
	summary {
		cursor: pointer;
		padding: 10px 0;
		font-weight: 600;
	}
	.replace {
		flex-direction: row;
		align-items: center;
	}
	.replace input {
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
