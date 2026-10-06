# Changelog

## 1.0.4
- Paquete `.deb`: campo `Maintainer` con los datos del autor y fichero `copyright` con la licencia MIT.
- Documentación completa (README, CHANGELOG, LICENSE).

## 1.0.3
- Dry-run más fiel: lo que ya se ha simulado archivar no vuelve a aparecer como candidato a truncado.

## 1.0.2
- Los ficheros de sysstat por día del mes (`sysstat/saNN`, `sysstat/sarNN`) de cualquier día distinto de hoy se archivan aunque su mtime sea reciente (el job de medianoche de sysstat los toca todos).

## 1.0.1
- Nunca se truncan ficheros binarios (`lastlog`, `wtmp`, `sa*`…): se detectan por bytes NUL.
- Nuevo paso de ficheros obsoletos (`STALE_HOURS`): se archivan los no rotados sin modificar desde hace N horas.
- No se mueve ningún fichero abierto por un proceso (comprobado en `/proc/*/fd`).
- Opción `--version`.

## 1.0.0
- Primera versión: servicio systemd (timer cada minuto) que vigila el espacio libre (bloques e inodos) de `/var/log`, archiva logs rotados 24 h y los purga; escalado a `journalctl --vacuum` y truncado de logs activos como último recurso.
- Paquete `.deb` (`Architecture: all`) generado con `build-deb.py` sin necesidad de `dpkg-deb`.
