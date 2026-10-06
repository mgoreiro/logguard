# logguard

Evita que `/var/log` se llene. Pensado para placas ARM (Orange Pi, Raspberry Pi…) con Armbian/Debian donde `/var/log` vive en RAM (zram o tmpfs, p. ej. con `armbian-ramlog` o `log2ram`) y por tanto es pequeño: cuando se llena, los servicios dejan de poder escribir logs.

Un timer de systemd ejecuta cada minuto un script Python 3 (solo librería estándar). Si el espacio libre cae por debajo de un umbral (15 % por defecto, configurable en `/etc/logguard.conf`), mueve los logs antiguos a una carpeta persistente fuera de `/var/log`, los conserva 24 horas y después los purga.

## Instalación

### Desde el paquete .deb (recomendado)

Descarga el `.deb` de la [última release](https://github.com/mgoreiro/logguard/releases/latest) e instálalo:

```bash
sudo apt install ./logguard_1.0.4_all.deb
```

Es `Architecture: all`: sirve igual para armhf y arm64. Depende de `python3 (>= 3.7)` y `systemd`; recomienda `logrotate`. Al instalar se activa y arranca `logguard.timer`. Al actualizar, tu `/etc/logguard.conf` se conserva.

Para desinstalar: `sudo apt remove logguard` (conserva configuración y archivo) o `sudo apt purge logguard` (borra también `/var/lib/logguard`).

### Desde el código fuente

```bash
git clone https://github.com/mgoreiro/logguard.git
cd logguard
sudo ./install.sh              # instala script, config (si no existe), unidades y activa el timer
sudo ./install.sh --uninstall
```

## Uso

```bash
logguard --status              # espacio libre y lotes archivados
logguard -n -v                 # simulación (dry-run) con salida detallada
logguard -c /ruta/otra.conf    # usar otro fichero de configuración
journalctl -u logguard         # histórico de acciones
systemctl status logguard.timer
```

El servicio no escribe nada en el journal mientras no haya nada que limpiar.

## Cómo funciona

En cada ejecución:

1. **Purga** los lotes archivados con más de `RETENTION_HOURS` (24 h), haya o no presión de espacio.
2. Mide el espacio libre de `LOG_DIR` (el **menor** entre bloques e inodos). Si es >= `MIN_FREE_PERCENT`, termina.
3. Si no, escala paso a paso, parando en cuanto se alcanza `TARGET_FREE_PERCENT`:
   1. **Logs rotados** (`*.1`, `*.2.gz`, `*.old`, `*-YYYYMMDD`, `*.gz|xz|zst|bz2`) → archivo, comprimidos con gzip. Primero los más antiguos.
   2. **Ficheros obsoletos**: no rotados, pero sin modificar desde hace `STALE_HOURS`, o ficheros por día de sysstat (`sysstat/saNN`, `sarNN`) de cualquier día que no sea hoy → archivo.
   3. `journalctl --vacuum-size` (solo si existe `/var/log/journal`).
   4. Opcional: `logrotate -f` (`LOGROTATE_FORCE=yes`) y archivado de lo generado.
   5. **Último recurso**: guarda la cola (`TAIL_KB`) de los logs activos de texto más grandes y los trunca. Los binarios (`lastlog`, `wtmp`, `sa*`…) nunca se truncan.
4. Sale con código 3 si tras todo sigue por debajo del mínimo (el servicio aparece como *failed* en systemd).

Garantías de seguridad:

- Nunca se mueve ni trunca un fichero abierto por un proceso (se comprueba en `/proc/*/fd`) salvo el truncado de último recurso, que solo afecta a logs de texto.
- El archivo guarda cada ejecución en un directorio `ARCHIVE_DIR/<AAAAMMDDTHHMMSSZ>/`, por lo que la purga no depende del mtime de los ficheros. Nunca se borran directorios del archivo que no tengan nombre de lote.
- Si el destino del archivo no tiene `ARCHIVE_MIN_FREE_MB` libres, no archiva (y el truncado no guarda la cola).
- Un `flock` en `/run/logguard.lock` evita ejecuciones solapadas.
- No se hace `fstrim` ni se mide la RAM de zram: ext4 reutiliza los bloques borrados aunque zram no los devuelva, que es lo único que importa para poder seguir escribiendo.

## Configuración

`/etc/logguard.conf` (formato `CLAVE=VALOR`, `#` para comentarios):

| Clave | Por defecto | Descripción |
|---|---|---|
| `MIN_FREE_PERCENT` | `15` | Umbral de libre (%) bajo el cual se limpia |
| `TARGET_FREE_PERCENT` | `25` | Se limpia hasta superar este libre (histéresis) |
| `LOG_DIR` | `/var/log` | Directorio vigilado |
| `ARCHIVE_DIR` | `/var/lib/logguard/archive` | Archivo temporal; debe ser persistente y estar fuera de `/var/log` y de `/var/log.hdd` |
| `RETENTION_HOURS` | `24` | Horas que se conserva cada lote |
| `ARCHIVE_MIN_FREE_MB` | `100` | No archivar si el destino tiene menos libre |
| `COMPRESS` | `gzip` | `gzip` o `none` |
| `JOURNAL_VACUUM_SIZE` | `20M` | Para `journalctl --vacuum-size`; vacío = omitir |
| `LOGROTATE_FORCE` | `no` | Ejecutar `logrotate -f` como paso extra |
| `STALE_HOURS` | `24` | Antigüedad para archivar no rotados; `0` = desactivar el paso |
| `TRUNCATE_ACTIVE` | `yes` | Permitir el truncado de último recurso |
| `TRUNCATE_MIN_KB` | `64` | Tamaño mínimo para considerar truncar |
| `TAIL_KB` | `256` | Cola que se guarda antes de truncar |
| `EXCLUDE` | *(vacío)* | Patrones fnmatch (separados por comas, relativos a `LOG_DIR`) que nunca se tocan, p. ej. `sysstat/*` |

## Desarrollo

```bash
python3 -m unittest discover -s tests   # tests unitarios (FS simulado, sin tocar /var/log)
python3 build-deb.py                    # genera dist/logguard_<versión>_all.deb
DEB_MAINTAINER="Nombre <correo>" python3 build-deb.py
```

`build-deb.py` construye el `.deb` solo con la librería estándar de Python (archivo `ar` + `tar.gz`), así que funciona en cualquier sistema sin `dpkg-deb`. La versión se lee de `__version__` en `logguard.py`.

Estructura:

```
logguard.py          script principal (se instala en /usr/sbin/logguard)
logguard.conf        configuración por defecto (/etc/logguard.conf)
systemd/             logguard.service y logguard.timer
packaging/           postinst, prerm, postrm del .deb
build-deb.py         constructor del .deb
install.sh           instalación/desinstalación sin paquete
tests/               tests unitarios
```

## Limitaciones conocidas

- Truncar un log activo solo es limpio si el proceso lo abre con `O_APPEND` (lo habitual en rsyslog y la mayoría de daemons). Si no, puede quedar un fichero disperso hasta que el proceso lo reabra.
- Con intervalo de 1 minuto, un proceso que escriba cientos de MB por minuto puede llenar `/var/log` entre dos ejecuciones; en ese caso sube `MIN_FREE_PERCENT` o corrige el origen del ruido.
- `armbian-truncate-logs` puede coexistir como segunda línea de defensa; logguard no lo modifica.
- Los datos de sysstat anteriores al día actual se archivan 24 h y luego se pierden; usa `EXCLUDE=sysstat/*` si quieres conservarlos.

## Autor

**mgoreiro** · <mgoreiro@gmail.com> · [github.com/mgoreiro](https://github.com/mgoreiro)

## Licencia

[MIT](LICENSE) © 2026 mgoreiro. Historial de cambios en [CHANGELOG.md](CHANGELOG.md).
