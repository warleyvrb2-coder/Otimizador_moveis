# -*- coding: utf-8 -*-
"""
Execução em segundo plano, bem simples.

Motivo de existir: o cálculo de um lote leva de segundos a alguns minutos.
Fazer isso dentro da requisição HTTP funciona na sua máquina e quebra em
qualquer hospedagem — o proxy corta a conexão muito antes de terminar, e o
usuário vê erro de rede depois de esperar. Então a requisição só ENFILEIRA
o trabalho e devolve um id; o navegador acompanha o progresso por polling.

Deliberadamente sem Celery/Redis: o volume aqui é uma pessoa rodando um lote
por vez. Uma thread e um dicionário resolvem, e não adicionam infraestrutura
pra manter. A contrapartida é que o estado vive na memória do processo —
por isso o servidor precisa rodar com UM worker só (veja o Procfile). Se um
dia virar multiusuário de verdade, isto aqui vira fila externa.
"""
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    id: str
    estado: str = 'na_fila'      # na_fila | rodando | pronto | erro
    feito: int = 0
    total: int = 1
    texto: str = 'Na fila...'
    resultado: dict | None = None
    erro: str | None = None
    criado_em: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def pct(self) -> int:
        return int(100 * self.feito / self.total) if self.total else 0

    def como_json(self) -> dict:
        return {'id': self.id, 'estado': self.estado, 'pct': self.pct,
                'texto': self.texto, 'erro': self.erro}


_jobs: dict[str, Job] = {}
_lock = threading.Lock()

# quantos jobs terminados guardamos antes de descartar os mais antigos
MAX_HISTORICO = 20


def criar(func, *args, ao_terminar=None, **kwargs) -> Job:
    """
    Enfileira func(*args, progresso=..., **kwargs) numa thread.

    ao_terminar(job) roda depois do sucesso, dentro da mesma thread. É o
    gancho que grava o resultado no banco - sem ele o plano viveria só aqui
    na memória e sumiria no próximo reinício.
    """
    job = Job(id=uuid.uuid4().hex[:10])
    with _lock:
        _jobs[job.id] = job
        _limpar()

    def progresso(feito, total, texto):
        job.feito, job.total, job.texto = feito, total, texto

    def alvo():
        job.estado = 'rodando'
        try:
            job.resultado = func(*args, progresso=progresso, **kwargs)
            job.feito = job.total
            if ao_terminar:
                try:
                    ao_terminar(job)
                except Exception:            # gravar falhou, mas o cálculo é válido
                    traceback.print_exc()
            job.estado = 'pronto'
            job.texto = 'Pronto'
        except Exception as e:                     # noqa: BLE001 - queremos mostrar qualquer falha
            job.estado = 'erro'
            job.erro = f'{type(e).__name__}: {e}'
            job.texto = 'Falhou'
            traceback.print_exc()

    threading.Thread(target=alvo, daemon=True, name=f'job-{job.id}').start()
    return job


def obter(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _limpar() -> None:
    """Descarta os jobs terminados mais antigos pra memória não crescer sem fim."""
    terminados = sorted((j for j in _jobs.values() if j.estado in ('pronto', 'erro')),
                        key=lambda j: j.criado_em)
    for j in terminados[:-MAX_HISTORICO] if len(terminados) > MAX_HISTORICO else []:
        _jobs.pop(j.id, None)


def todos() -> list[Job]:
    """Todos os jobs que ainda estão na memória, pra tela de histórico."""
    return list(_jobs.values())
