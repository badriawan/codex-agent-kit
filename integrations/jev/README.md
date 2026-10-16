# Jev MCP opsional

Integrasi ini memberi saran delegasi melalui `route_work` dan penilaian perubahan
melalui `review_change`. Instalasi inti kit **tidak mengaktifkan Jev**. Tambahkan
konfigurasi berikut secara manual hanya jika Anda ingin memakainya.

File server, pengaturan, dependensi langsung, dan tes mempertahankan integrasi
asal: model `jev-1.13.0`, `typesafe-sdk==0.7.5`, dan `mcp==2.3.0`.
Dependensi transitif belum dikunci. Gunakan Python 3.11 atau lebih baru dan jaringan
untuk memasang paket. Setiap perangkat perlu membuat virtual environment sendiri;
jangan menyalin `.venv` dari perangkat lain.

## Persiapan

Dari direktori `integrations/jev` pada clone Anda, jalankan salah satu berikut.

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v test_server.py
.venv/bin/python check_connection.py
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest -v test_server.py
.\.venv\Scripts\python.exe check_connection.py
```

Pemeriksaan di atas berjalan offline setelah pemasangan dependensi: tes memakai
mock dan `check_connection.py` hanya memeriksa discovery MCP. Opsi `--live`
melakukan dua permintaan API dan dapat menimbulkan biaya; gunakan hanya jika
Anda memang ingin memeriksa koneksi langsung.

## API key dan konfigurasi Codex

Sediakan environment variable **`TYPESAFE_API_KEY`** pada proses yang menjalankan
Codex, melalui pengelola secret atau konfigurasi lingkungan perangkat Anda.
Jangan masukkan nilainya ke repository, `AGENTS.md`, atau berkas konfigurasi ini.
Aplikasi desktop yang dibuka dari launcher mungkin tidak mewarisi environment
terminal; pastikan variabel tersedia pada proses Codex yang sebenarnya.

Alternatif untuk aplikasi desktop: `mcp_servers.jev.env` dapat memasok
`TYPESAFE_API_KEY` langsung ke proses server. Masukkan nilainya hanya ke
`config.toml` pribadi di luar repository, dengan permission privat (mode `600`
di POSIX). Jangan menyalin konfigurasi yang berisi kunci ke repo atau perangkat
lain; setiap perangkat perlu menyiapkan kredensialnya sendiri.

Tambahkan tabel berikut ke `config.toml` di direktori konfigurasi Codex Anda
(umumnya `~/.codex/config.toml`, atau lokasi `CODEX_HOME` Anda). Jika tabel
`mcp_servers.jev` sudah ada, sesuaikan tabel itu agar tidak duplikat.
Ganti kedua path contoh dengan **path absolut** clone pada perangkat ini.

macOS / Linux:

```toml
[mcp_servers.jev]
command = "/ABSOLUTE/PATH/codex-agent-kit/integrations/jev/.venv/bin/python"
args = ["/ABSOLUTE/PATH/codex-agent-kit/integrations/jev/server.py"]
env_vars = ["TYPESAFE_API_KEY"]
enabled_tools = ["route_work", "review_change"]
startup_timeout_sec = 20
tool_timeout_sec = 25
```

Windows (gunakan garis miring `/` dalam string TOML):

```toml
[mcp_servers.jev]
command = "C:/ABSOLUTE/PATH/codex-agent-kit/integrations/jev/.venv/Scripts/python.exe"
args = ["C:/ABSOLUTE/PATH/codex-agent-kit/integrations/jev/server.py"]
env_vars = ["TYPESAFE_API_KEY"]
enabled_tools = ["route_work", "review_change"]
startup_timeout_sec = 20
tool_timeout_sec = 25
```

Mulai ulang sesi Codex setelah konfigurasi berubah. Kedua tool seharusnya tersedia
di server MCP `jev`. Untuk menonaktifkan, tambahkan `enabled = false` pada tabel
tersebut. `settings.toml` mengatur batas waktu API 15 detik, batas masukan 12.000
karakter, dan ambang advisory sementara 0,70. Server tidak melakukan retry API.

## Skill Jev

Salinan skill tersedia di `skill/jev/SKILL.md`. Untuk memasangnya, jalankan dari
folder `integrations/jev` setelah MCP dikonfigurasi.

macOS / Linux (sesuaikan tujuan bila memakai `CODEX_HOME`):

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skill/jev "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Windows PowerShell:

```powershell
$jevSkillsPath = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex/skills' }
New-Item -ItemType Directory -Force -Path $jevSkillsPath | Out-Null
Copy-Item -Recurse skill/jev $jevSkillsPath
```

Jika skill `jev` sudah ada, periksa edit lokal sebelum menyalin versi baru.
Mulai sesi Codex baru; gunakan `$jev` untuk meminta saran delegasi atau penilaian
perubahan. Skill juga dapat dipilih otomatis saat tugas cocok dengan deskripsinya.
Skill ini khusus memakai advisor MCP kit; skill vendor `typesafe-ai` tetap
digunakan untuk membangun aplikasi berbasis TypeSafe.

## Batas dan penggunaan

- Permintaan tool mengirim ringkasan yang Anda berikan ke TypeSafe dan dapat
  dikenai biaya API terpisah. Kirim hanya ringkasan singkat tanpa secret atau
  kredensial; server tidak membaca repository untuk menyusun masukan.
- `route_work` menyarankan satu peran yang tersedia; tool ini tidak membuat agen.
  Pemanggilan dan ketersediaan peran tetap mengikuti kemampuan host Codex.
- `review_change` menilai rencana, ringkasan perubahan, dan hasil validasi yang
  dilaporkan pemanggil. Tool ini tidak membuka berkas atau menjalankan tes.
- Hasil adalah saran dan probabilitas, bukan izin untuk mengeksekusi, merge,
  menerbitkan, atau melewati review architect yang diwajibkan.
- Jika API gagal, tidak ada key, atau hasil tidak pasti, gunakan penilaian agen
  utama dan pertahankan review architect yang diwajibkan. Jangan mengulang
  permintaan berkali-kali hanya untuk mendapat saran yang diinginkan.

Dokumentasi vendor: [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python),
[Jev dengan coding agents](https://docs.typesafe.ai/introduction/coding-agents).
