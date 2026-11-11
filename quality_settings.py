"""UVR-style inference controls shared by the CUDA and MLX front ends."""
import math

PRESETS = {
    'Fast': dict(segment_size=128, overlap=2, overlap_ratio=.25, demucs_segment=5, demucs_shifts=1, vr_tta=False),
    'Balanced': dict(segment_size=256, overlap=4, overlap_ratio=.5, demucs_segment=None, demucs_shifts=2, vr_tta=False),
    'High': dict(segment_size=None, overlap=8, overlap_ratio=.75, demucs_segment=None, demucs_shifts=4, vr_tta=True),
}
HELP = ('Overlap blends predictions from neighboring windows: more overlap can reduce boundary artifacts but takes longer. '
        'Segment size controls how much audio a model sees at once: larger segments use more memory and can change the result. '
        'Model keeps the Roformer/MDXC checkpoint\'s configured context; MDX uses its standard 256 frames. '
        'High uses model context and more overlap; it is not guaranteed to sound better on every song. '
        'On a 6 GB card, start with Balanced and compare a short excerpt. '
        'These controls affect inference, independently of file format or published SDR scores.')


def validate(settings):
    value = dict(settings)
    for key, low, high in (('segment_size', 32, 4096), ('overlap', 2, 50), ('demucs_shifts', 0, 8), ('demucs_segment', 1, 7)):
        n = value.get(key)
        if key in ('segment_size', 'demucs_segment') and n is None:
            continue
        if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
    ratio = value.get('overlap_ratio')
    if isinstance(ratio, bool) or not isinstance(ratio, (float, int)) or not math.isfinite(ratio) or not 0 < ratio <= .99:
        raise ValueError('MDX/Demucs overlap must be between 0 and 0.99 (exclusive of zero)')
    if not isinstance(value.get('vr_tta'), bool):
        raise ValueError('VR augmentation must be on or off')
    return value


def add_arguments(parser):
    parser.add_argument('--quality', choices=['fast', 'balanced', 'high'], default='balanced')
    parser.add_argument('--segment-size', help='Roformer/MDXC/MDX frames (32–4096), or model')
    parser.add_argument('--overlap', type=int, help='Roformer/MDXC overlap count (2–50)')
    parser.add_argument('--overlap-ratio', type=float, help='MDX/Demucs overlap fraction (0–0.99)')
    parser.add_argument('--demucs-segment', help='Demucs seconds (1–7), or model')
    parser.add_argument('--demucs-shifts', type=int)
    parser.add_argument('--vr-tta', choices=['on', 'off'])


def from_arguments(args):
    value = dict(PRESETS[args.quality.capitalize()])
    for key in value:
        n = getattr(args, key, None)
        if n is not None:
            if key in ('segment_size', 'demucs_segment'):
                n = None if str(n).lower() == 'model' else int(n)
            elif key == 'vr_tta':
                n = n == 'on'
            value[key] = n
    return validate(value)


def cli_args(value):
    value = validate(value)
    result = []
    for key, n in value.items():
        if key not in PRESETS['Balanced']:
            continue
        if key == 'vr_tta':
            n = 'on' if n else 'off'
        result += ['--' + key.replace('_', '-'), 'model' if n is None else str(n)]
    return result


def engine_params(value):
    """The native API uses a count for MDXC and a fraction for MDX/Demucs."""
    value = validate(value)
    frames = value['segment_size'] or 256
    return {
        'mdxc_params': dict(segment_size=frames, override_model_segment_size=value['segment_size'] is not None,
                            batch_size=1, overlap=value['overlap'], pitch_shift=0),
        'mdx_params': dict(hop_length=1024, segment_size=frames, overlap=value['overlap_ratio'], batch_size=1, enable_denoise=False),
        'demucs_params': dict(segment_size=str(value['demucs_segment'] or 'Default'), shifts=value['demucs_shifts'],
                              overlap=value['overlap_ratio'], segments_enabled=True),
        'vr_params': dict(batch_size=1, window_size=512, aggression=5, enable_tta=value['vr_tta'],
                          enable_post_process=False, post_process_threshold=.2, high_end_process=False),
    }


def mlx_cli_args(value):
    params = engine_params(value)
    result = []
    for prefix, names in (('mdx', ('segment_size', 'overlap')), ('demucs', ('segment_size', 'overlap', 'shifts'))):
        for name in names:
            result += [f'--{prefix}_{name}', str(params[prefix + '_params'][name])]
    if value['vr_tta']:
        result += ['--vr_enable_tta']
    return result


def apply_compat_config(config, value):
    """HyperACE/SCNet use sample counts, not the stock engine's frame units."""
    value = validate(value)
    if value['segment_size'] is not None:
        hop = config.model.get('stft_hop_length', config.model.get('hop_size', 512))
        config.audio.chunk_size = (value['segment_size'] - 1) * hop
    config.inference.num_overlap = value['overlap']
    config.inference.batch_size = 1


def apply_mlx_mdxc(instance, value):
    """MLX 0.1.20 interprets ordinary Roformer overlap as stride in seconds.

    Translate UVR's count explicitly; passing the same number directly would
    reduce overlap instead of increasing it. The HF path uses count/sample units.
    """
    data = getattr(instance, 'model_data', {})
    if not data:
        return
    value = validate(value)
    audio, model = data.get('audio', {}), data.get('model', {})
    inference = data.setdefault('inference', {})
    hop = model.get('stft_hop_length', audio.get('hop_length', 512))
    hf = data.get('backend') == 'huggingface_mel_roformer'
    frames = value['segment_size']
    if frames is None:
        frames = inference.get('dim_t', getattr(instance, 'segment_size', 256))
    chunk = hop * (frames - 1)
    if hf:
        chunk = inference['chunk_size'] if value['segment_size'] is None else chunk
        inference['chunk_size'] = chunk
        inference['num_overlap'] = value['overlap']
    else:
        instance.segment_size = frames
        instance.override_model_segment_size = True
        instance.overlap = chunk / audio.get('sample_rate', instance.sample_rate) / value['overlap']
    instance.batch_size = 1


class QualityControls:
    def __init__(self, parent, saved, on_save):
        import tkinter as tk
        from tkinter import ttk
        self.on_save, self.architecture = on_save, 'MDXC'
        try:
            self.value = validate(saved['values'])
            name = saved['preset'] if saved['preset'] in (*PRESETS, 'Custom') else 'Custom'
        except (KeyError, TypeError, ValueError):
            self.value, name = dict(PRESETS['Balanced']), 'Balanced'
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill='x', pady=(4, 2))
        ttk.Label(self.frame, text='Quality:').pack(side='left')
        self.preset = tk.StringVar(value=name)
        self.combo = ttk.Combobox(self.frame, textvariable=self.preset, values=[*PRESETS, 'Custom'], state='readonly', width=12)
        self.combo.pack(side='left', padx=6)
        self.combo.bind('<<ComboboxSelected>>', self._preset_changed)
        ttk.Button(self.frame, text='Advanced…', command=self.advanced).pack(side='left')
        self.summary = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.summary).pack(side='left', padx=10)
        self._summarize()

    def _summarize(self):
        v = self.value
        if self.architecture == 'MDX':
            text = f"{v['segment_size'] or 256} frames · {v['overlap_ratio']:.0%} overlap"
        elif self.architecture == 'Demucs':
            text = f"{str(v['demucs_segment']) + 's' if v['demucs_segment'] else 'Model segment'} · {v['overlap_ratio']:.0%} overlap · {v['demucs_shifts']} shifts"
        elif self.architecture == 'VR':
            text = 'Test-time augmentation ' + ('on' if v['vr_tta'] else 'off')
        else:
            segment = str(v['segment_size']) + ' frames' if v['segment_size'] else 'Model segment'
            text = f"{segment} · overlap {v['overlap']}"
        self.summary.set(text)

    def set_architecture(self, architecture):
        self.architecture = architecture
        self._summarize()

    def _save(self):
        self._summarize()
        self.on_save({'preset': self.preset.get(), 'values': dict(self.value)})

    def _preset_changed(self, _event=None):
        name = self.preset.get()
        if name == 'Custom':
            self.advanced()
        else:
            self.value = dict(PRESETS[name])
            self._save()

    def engine_args(self):
        return cli_args(self.value)

    def advanced(self):
        import tkinter as tk
        from tkinter import ttk
        dialog = tk.Toplevel(self.frame)
        dialog.title('Separation quality — ' + self.architecture)
        dialog.transient(self.frame.winfo_toplevel())
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill='both', expand=True)
        fields = {}
        def field(key, label, values):
            row = len(fields)
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=4, padx=(0, 16))
            current = self.value[key]
            if key == 'vr_tta':
                current = 'On' if current else 'Off'
            fields[key] = tk.StringVar(value='Model' if current is None else str(current))
            ttk.Combobox(body, textvariable=fields[key], values=values, state='readonly', width=18).grid(row=row, column=1, sticky='ew')
        if self.architecture in ('MDXC', 'MDX'):
            sizes = (['Model'] if self.architecture == 'MDXC' else []) + [str(i) for i in range(32, 4097, 32)]
            field('segment_size', 'Segment size (frames)', sizes)
            if self.architecture == 'MDX' and fields['segment_size'].get() == 'Model':
                fields['segment_size'].set('256')
        if self.architecture == 'MDXC':
            field('overlap', 'Overlap (prediction count)', list(range(2, 51)))
        if self.architecture in ('MDX', 'Demucs'):
            field('overlap_ratio', 'Overlap (fraction; 0.5 = 50%)', [.25, .5, .75, .9, .95, .99])
        if self.architecture == 'Demucs':
            field('demucs_segment', 'Segment size (seconds)', ['Model', *range(1, 8)])
            field('demucs_shifts', 'Shift averaging (passes)', list(range(9)))
        if self.architecture == 'VR':
            field('vr_tta', 'Test-time augmentation', ['Off', 'On'])
        row = len(fields)
        ttk.Label(body, text=HELP, wraplength=490, justify='left').grid(row=row, column=0, columnspan=2, pady=(14, 10))
        buttons = ttk.Frame(body)
        buttons.grid(row=row + 1, column=0, columnspan=2, sticky='e')
        def apply():
            value = dict(self.value)
            for key, var in fields.items():
                raw = var.get()
                value[key] = (raw == 'On') if key == 'vr_tta' else (None if raw == 'Model' else float(raw) if key == 'overlap_ratio' else int(raw))
            self.value = validate(value)
            self.preset.set('Custom')
            self._save()
            dialog.destroy()
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='left', padx=6)
        ttk.Button(buttons, text='Apply', command=apply).pack(side='left')
