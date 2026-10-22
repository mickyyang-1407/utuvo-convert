# Known limits

- Apple Silicon/macOS development build. The original signed app and this
  source snapshot have separate release/verification histories.
- Ordinary decoded PCM has a 512 MiB safety cap. Some long multichannel files
  will be refused. Unsupported ADM structures fail with a diagnostic.
- Channel count alone is not a channel layout. Confirm ambiguous input orders.
- Experimental upmix produces estimated PCM channels, not Atmos objects, ADM
  export, Dolby Digital Plus JOC, TrueHD or DTS:X.
- Normalization is off. No dither is added during bit-depth reduction.
- MP3 reconstruction can exceed 0 dBFS; warnings preserve the true decoded peak.
  QC PASS covers format, duration, sample rate, channels and finite samples,
  not a guarantee of playback headroom.
- Descriptive source tags are not copied. Output channel/QC data is stored in
  a sidecar JSON report.
- Authored glass frames are pre-rendered, not live desktop refraction.
- Physical speaker compatibility, listening quality, other Macs and the full
  accessibility matrix require separate acceptance.
