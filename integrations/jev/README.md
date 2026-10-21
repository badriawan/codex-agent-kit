# Jev: saran peran subagen

Workflow yang digunakan adalah **primary → Jev → validasi kode MCP → primary
memanggil subagen → primary memverifikasi hasil**. Model dan reasoning effort
setiap custom role tetap terkunci. Jev tidak membuat agen atau menyetujui izin.

Server `role_server.py` berdiri sendiri dan hanya menyediakan `route_work`.
Ia tidak mengimpor server routing lama, tidak membuat SQLite routing state,
dan tidak memasang hook tindakan. `server.py`, `routing.py`, dan tes lamanya
dipertahankan untuk kompatibilitas mode legacy; jangan gunakan mode tersebut
untuk workflow ini.

## Kontrak tool

```json
{
  "task_summary": "Read existing code and identify the argument parsing entrypoint; no edits.",
  "available_roles": ["explorer", "researcher", "worker", "architect"]
}
```

Primary memberikan ringkasan singkat tanpa secret serta subset role yang benar-benar
tersedia dan diizinkan. Daftar harus unik, tidak kosong, dan hanya berisi nama
canonical tersebut. `main` ditambahkan server sebagai pilihan abstain. Server
memvalidasi format, pilihan, distribusi probabilitas, dan confidence; ketersediaan
runtime berasal dari informasi primary, bukan discovery independen MCP.

Hasil yang dapat diteruskan memiliki `validated=true`, `disposition="selected"`,
serta `selected_role` dan `agent_type` yang sama. Primary memakai `agent_type`
untuk native `spawn_agent` dengan tugas lengkap, tanpa menilai ulang pemilihan
peran. Jangan memberikan override model/effort. Runtime tetap memeriksa izin dan
slot; primary tetap memeriksa otorisasi tugas dan perubahan konteks.

Untuk input/hasil tidak valid, `main`, confidence rendah, timeout, atau API gagal,
hasil memiliki `disposition="main"`, `validated=false`, tanpa `agent_type`.
Primary memutuskan sekali tanpa mengulang Jev. Tugas sederhana dikerjakan primary;
checkpoint architect wajib langsung mengikuti aturan.

## Pasang dan aktifkan

Gunakan Python 3.11+. Buat environment per perangkat; jangan menyalin `.venv`.
Dari direktori ini:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v test_role_server.py
.venv/bin/python check_role_connection.py
```

Siapkan `TYPESAFE_API_KEY` pada lingkungan proses Codex. Konfigurasi pribadi
`mcp_servers.jev.env.TYPESAFE_API_KEY` juga didukung; simpan hanya pada file privat,
bukan repository atau ringkasan permintaan. Kredensial tidak dipindahkan antarperangkat.
Dari root kit, gunakan interpreter kit yang memiliki `tomlkit`:

```sh
.venv/bin/python configure_runtime.py install --jev-roles-only --jev-python "$PWD/integrations/jev/.venv/bin/python" --dry-run
.venv/bin/python configure_runtime.py install --jev-roles-only --jev-python "$PWD/integrations/jev/.venv/bin/python"
.venv/bin/python configure_runtime.py status
```

Installer mencadangkan perubahan secara privat, mempertahankan kredensial dan
setting lain, serta memigrasikan hook milik Jev dari mode legacy. Kondisi legacy
yang sengaja dinonaktifkan (`enabled=false`, hook Jev dilepas) ditangani secara
terbatas; edit lain pada nilai yang dikelola tetap ditolak. Role-only tidak
memerlukan `/hooks` trust. Auto-review adalah pengaturan terpisah.
Installer menolak `hooks.json`, hook Jev tanpa kepemilikan installer, dan original
hook Jev yang akan dipulihkan saat migrasi. Selesaikan konflik tersebut sebelum
mengaktifkan role-only; hook layanan lain tetap dipertahankan.

Salin `skill/jev/SKILL.md` ke folder skill Jev perangkat, dan gunakan bagian
Jev role-only dari `config/AGENTS.md` pada aturan aktif. Mulai **sesi baru** agar
server, allowlist tool, dan metadata skill termuat ulang. Jangan menganggap sesi
lama sudah memakai workflow baru hanya karena file config berubah.

Konfigurasi MCP yang dihasilkan:

```toml
[mcp_servers.jev]
command = "/ABSOLUTE/PATH/integrations/jev/.venv/bin/python"
args = ["/ABSOLUTE/PATH/integrations/jev/role_server.py"]
enabled = true
enabled_tools = ["route_work"]
env_vars = ["TYPESAFE_API_KEY"]
```

Untuk Windows gunakan path absolut interpreter `Scripts/python.exe`. Jangan
menjalankan opsi legacy `--jev`: opsi tersebut memasang routing tindakan dan hook.

## Verifikasi

`check_role_connection.py` memeriksa discovery tepat satu tool dan fallback role
tidak valid tanpa API. Satu percobaan live opsional memakai ringkasan sintetis:

```sh
.venv/bin/python check_role_connection.py --live
```

Bila key hanya ada pada config pribadi, tambahkan `--codex-home /path/to/.codex`.
Opsi ini hanya membaca key untuk child process dan tidak mencetak nilainya.
`--live` mengirim satu permintaan TypeSafe dan dapat menimbulkan biaya API.
Hasil checker adalah bukti subprocess MCP/API, bukan bukti sesi host sudah reload
atau native `spawn_agent` sudah dijalankan.

`settings.toml` mempertahankan `jev-1.14.0`, timeout API 15 detik, batas input
12.000 karakter, dan threshold confidence sementara 0,70. Retry API otomatis 0.
Threshold bukan ukuran akurasi workflow. Hanya ringkasan dan metadata role yang
dikirim ke TypeSafe; full prompt, isi file, output, serta konfigurasi tidak dikirim.

Tes offline menggunakan fake provider dan meliputi validasi, fallback, timeout,
kontrak SDK, discovery MCP, dan isolasi dari modul routing lama.
