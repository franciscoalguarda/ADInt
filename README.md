# ADInt — Projeto Parte 1

Implementação da primeira parte do projeto de gestão de exames nacionais.
Cada serviço é uma aplicação Python independente, usa a sua própria base de
dados SQLite e pode ser iniciado separadamente.

## Executar

Na raiz do repositório, criar o ambiente virtual e instalar as dependências:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r user_service\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r admin_service\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r printing_service\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r correction_service\requirements.txt
```

Iniciar cada aplicação numa janela PowerShell diferente, sempre a partir da
raiz:

```powershell
.\.venv\Scripts\python.exe user_service\su_app.py
.\.venv\Scripts\python.exe admin_service\as_app.py
.\.venv\Scripts\python.exe printing_service\ps_app.py
.\.venv\Scripts\python.exe correction_service\cs_app.py
```

| Serviço | URL | Base de dados |
| --- | --- | --- |
| User Service | <http://127.0.0.1:5001> | `user_service\users.sqlite` |
| Admin Service | <http://127.0.0.1:5002> | `admin_service\admin.sqlite` |
| Printing Service | <http://127.0.0.1:5003> | `printing_service\printing.sqlite` |
| Correction Service | <http://127.0.0.1:5004> | `correction_service\corrections.sqlite` |

As aplicações correm em modo de desenvolvimento. Na Parte 1 não há
autenticação. Cada serviço persiste apenas os dados da sua responsabilidade.

## Funcionalidades, páginas e endpoints

### User Service

Implementa SU_1–SU_3: listar utilizadores, adicionar um utilizador e importar
vários utilizadores de um CSV UTF-8. Os papéis permitidos são Administrator,
Professor, Exam invigilator e Student; as passwords são guardadas como hashes.

| Endpoint | Métodos | Funcionalidade |
| --- | --- | --- |
| `/` | GET | Página inicial e navegação. |
| `/list_users` | GET | SU_1: listar utilizadores e papéis. |
| `/list_users/sucess` | GET | Listar utilizadores com confirmação de adição ou importação. |
| `/users/add` | GET, POST | SU_2: adicionar um utilizador. |
| `/users/bulk-add` | GET, POST | SU_3: importação atómica de utilizadores por CSV. |

Modelos SQLAlchemy em `user_service/user_data.py`: `User`, `Role` e a tabela
de associação `UserRoles` (`user_roles`). Esta estrutura normalizada suporta
mais do que um papel por utilizador.

### Admin Service

Implementa AD_1–AD_3 e ST_1: agendar e listar exames, inscrever alunos por CSV,
listar inscrições e pedir a impressão das folhas. A lista CSV de inscrições
deve conter a coluna `student_id` (ou `student_identifier`). O pedido de
impressão chama o Printing Service por XML-RPC; não há acesso cruzado às bases
de dados dos serviços.

| Endpoint | Métodos | Funcionalidade |
| --- | --- | --- |
| `/` | GET | Resumo do serviço. |
| `/exams/schedule` | GET, POST | AD_1: agendar um exame. |
| `/exams` | GET | Listar exames e aceder às ações de inscrição/impressão. |
| `/exams/<exam_id>/enroll` | GET, POST | AD_2: inscrever alunos por CSV. |
| `/enrollments` | GET | ST_1: listar inscrições e respetivos exames. |
| `/exams/<exam_id>/order-printing` | POST | AD_3: enviar pedido XML-RPC. |

Modelos SQLAlchemy em `admin_service/admin_data.py`: `Exam` e `Enrollment`.

### Printing Service

Recebe `print_exam(exam_id, exam_date, course, student_ids)` por XML-RPC e
transporta os identificadores como texto decimal para suportar inteiros de
64 bits, mantendo-os como inteiros na base de dados. O serviço
gera um único PDF com exatamente quatro páginas por aluno. Cada folha tem um
identificador único em QR code, igual nas suas quatro páginas. A página web
lista os exames impressos e disponibiliza os PDFs.

| Endpoint | Métodos | Funcionalidade |
| --- | --- | --- |
| `/` ou `/exams` | GET | Listar exames impressos. |
| `/RPC2` | POST XML-RPC | Receber pedidos de impressão. |
| `/exams/<exam_id>/pdf` | GET | Descarregar o PDF do exame. |

O PDF usa `printing_service\Exam-template.pdf`. O serviço usa
Flask-XML-RPC-Reloaded, um fork compatível com Python 3 da extensão
Flask-XML-RPC referida nas aulas. Modelos em
`printing_service/printing_data.py`: `PrintedExam` e `ResolutionSheet`.

### Correction Service

Implementa AD_4–AD_5 e PR_1–PR_3, bem como a vista parcial ST_3. Recebe uma
digitalização PDF, valida o número de páginas, separa as respostas por questão
e oculta os QR codes nas imagens disponibilizadas para correção. A Parte 1 não
associa a identidade do aluno à folha nem filtra listas por utilizador.

| Endpoint | Métodos | Funcionalidade |
| --- | --- | --- |
| `/` | GET | Resumo de digitalizações, respostas pendentes, progresso e atalhos de correção. |
| `/assignments` | GET, POST | AD_4: atribuir professor a questão de exame. |
| `/assignments/success` | GET | Confirmar a atribuição de professor. |
| `/scans/upload` | GET, POST | AD_5: carregar e processar uma digitalização. |
| `/answers` | GET | PR_1: listar respostas por corrigir. |
| `/answers/<id>/correct` | GET, POST | PR_2: consultar e guardar uma correção. |
| `/answers/corrected` | GET | PR_3: listar respostas corrigidas. |
| `/answers/corrected/success` | GET | Confirmar a gravação de uma correção. |
| `/scans` | GET | ST_3 parcial: listar PDFs e respostas. |
| `/scans/success` | GET | Confirmar o upload e apresentar as digitalizações. |
| `/answers/<id>/page.pdf` | GET | Consultar a página processada em PDF. |
| `/answers/<id>/page.png` | GET | Consultar a imagem processada. |
| `/scans/<id>/original.pdf` | GET | Descarregar o PDF original. |

Modelos SQLAlchemy em `correction_service/correction_data.py`:
`ProfessorAssignment`, `ExamCorrectionPdf` e `QuestionCorrection`.

## Testes

Os testes usam bases de dados e diretórios temporários:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s user_service -p "test*.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s admin_service -p "test*.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s printing_service -p "test*.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s correction_service -p "test*.py" -v
```

## Limpar bases de dados, caches e PDFs gerados

Parar os serviços que serão limpos e executar, na raiz do projeto:

| Limpeza | Comando |
| --- | --- |
| Bases de dados, caches e output dos quatro serviços | `python clean.py` |
| Apenas bases de dados dos quatro serviços | `python clean.py --databases-only` |
| Bases de dados, caches e output de um serviço | `python clean.py --service NOME_DO_SERVICO` |
| Apenas a base de dados de um serviço | `python clean.py --databases-only --service NOME_DO_SERVICO` |
| Apenas a pasta `printing_service/output` | `python clean.py --output-only` |

Substituir `NOME_DO_SERVICO` pelo *nome do serviço*: `user_service`,
`admin_service`, `printing_service` ou `correction_service`.
A opção `--service` pode ser repetida para selecionar vários serviços.
Acrescentar `--dry-run` a qualquer comando para simular sem apagar ficheiros.

A limpeza de bases de dados apaga os dados e os ficheiros auxiliares SQLite.
As caches são as pastas `__pycache__`; o output aplica-se apenas ao Printing
Service. `--output-only` preserva os PDFs guardados na base de dados e não pode
ser combinado com `--databases-only` ou com outro serviço.

O código, o ambiente virtual e o `Exam-template.pdf` são preservados.
As bases de dados, caches e output são recriados quando necessários.
Diretórios personalizados com `PRINTING_OUTPUT_DIR` não são limpos.

## Âmbito da Parte 1

A Parte 1 não inclui autenticação, associação do aluno à folha pelo invigilador,
notas finais agregadas nem o portal unificado. Estas funcionalidades pertencem
às partes seguintes da arquitetura descrita no enunciado.
