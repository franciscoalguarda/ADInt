# Printing Service

Serviço Flask com uma página de consulta e métodos XML-RPC para
gerar folhas de resolução. Cada aluno tem quatro páginas de resposta com o
mesmo identificador único em QR code.

## Executar

Na raiz do projeto, com o ambiente virtual `venv`:

```powershell
.\venv\Scripts\python.exe -m pip install -r printing_service\requirements.txt
.\venv\Scripts\python.exe printing_service\ps_app.py
```

Abrir <http://127.0.0.1:5003>. O endpoint XML-RPC é `/RPC2`; o POST na raiz
também é aceite por compatibilidade com o endereço predefinido no Admin.
É utilizada a extensão `Flask-XML-RPC-Reloaded`, compatível com Python 3.

## Páginas e comunicação

| URL | Métodos | Função |
| --- | --- | --- |
| `/` ou `/exams` | GET | Listagem de exames, número de folhas e páginas e ligação ao PDF atual. |
| `/RPC2` ou `/` | POST XML-RPC | Receber pedidos `print_exam`. |
| `/exams/<exam_id>/pdf` | GET | Consultar o PDF guardado na base de dados. |
| `/exams/<exam_id>/pdf/save` | POST | Guardar o PDF atual na pasta de output do serviço. |
| `/exams/pdf/saved` | GET | Confirmar a gravação e apresentar a listagem. |

A página inicial apresenta uma tabela com o número de folhas e páginas e
ligações `Open PDF` para visualizar no navegador e botões `Save to output` para
guardar a versão atual em `printing_service/output/exam_<exam_id>.pdf`.
O botão grava no computador onde corre o Printing Service e apresenta uma
confirmação na listagem, sem iniciar uma transferência pelo navegador.
Se `PRINTING_OUTPUT_DIR` estiver definido, usa essa pasta. A pasta é recriada
se tiver sido removida e um ficheiro existente é substituído pela versão atual.
Guardar esta cópia não gera novas folhas nem altera os QR codes.
Sem PDFs, apresenta as instruções para pedir a primeira impressão no Admin.

`print_exam(exam_id, exam_date, course, student_ids)` recebe o identificador do
exame, a data no formato `YYYY-MM-DD`, a unidade curricular e a lista de alunos.
Os IDs aceitam inteiros ou textos decimais positivos até `9223372036854775807`.
O Admin envia texto decimal para evitar o limite de 32 bits dos inteiros XML-RPC.
IDs inválidos, repetidos ou fora do intervalo são rejeitados.

Não é possível gerar ou atualizar folhas depois da data do exame, incluindo
em chamadas XML-RPC diretas. O próprio dia continua permitido. Os PDFs já
guardados continuam disponíveis para consulta.

## PDFs e QR codes

O modelo é `printing_service/Exam-template.pdf`. A primeira página de instruções
é omitida; as quatro páginas de questões são copiadas para cada aluno. O PDF
tem exatamente `4 × número de alunos` páginas.

Cada par exame/aluno mantém a mesma folha e o mesmo identificador em pedidos
posteriores. Repetir um pedido sem alterações devolve o PDF existente, com os
mesmos QR codes. Num novo pedido após inscrever mais alunos, são criadas apenas as novas folhas:
dois alunos produzem oito páginas e quatro alunos produzem dezasseis.

Pedidos com listas anteriores não removem alunos nem invalidam QR codes já
emitidos. Os novos alunos são acrescentados depois das folhas existentes.
Pedidos simultâneos são serializados no SQLite para preservar os identificadores
e a versão mais recente do PDF.

O PDF só é gerado ou atualizado através de `Order printing` no Admin Service.
Novas inscrições, por si só, não alteram o PDF existente. `Save to output`
guarda uma cópia do PDF já gerado, sem consultar as inscrições do Admin.
A comunicação entre serviços usa XML-RPC. Cada serviço mantém a sua própria base de dados.

## Dados e configuração

`printing_service/printing.sqlite` guarda os modelos `PrintedExam` e
`ResolutionSheet`, definidos em `printing_data.py`: exames, PDFs e associações
entre folhas, exames e alunos. Os identificadores das folhas são inteiros
gerados pelo SQLite e não são reutilizados.

O PDF também é escrito em `printing_service/output/exam_<exam_id>.pdf` como
cópia da versão guardada na base. A página de consulta disponibiliza a versão
da base de dados. As atualizações da cópia usam um ficheiro temporário para
evitar publicar PDFs incompletos.
Se a confirmação da transação falhar depois de gravar a cópia, o serviço
repõe o PDF confirmado na base de dados. A recuperação obtém um novo bloqueio
de escrita e consulta a versão atual, para não substituir um pedido mais recente.
Se falhar o primeiro pedido e não existir exame confirmado, remove a cópia.

Configuração opcional:

- `PRINTING_HOST`: endereço do servidor; por omissão, `127.0.0.1`.
- `PRINTING_PORT`: porta HTTP e XML-RPC; por omissão, `5003`.
- `PRINTING_DATABASE_URL`: ligação SQLAlchemy à base de dados.
- `PRINTING_OUTPUT_DIR`: diretoria para a cópia dos PDFs gerados.

Reiniciar o Admin e o Printing Service depois de atualizar o código.

## Testes

```powershell
.\venv\Scripts\python.exe -m unittest discover -s printing_service -v
```

Os testes usam bases e diretórios temporários. Verificam a geração dos PDFs,
os QR codes estáveis, pedidos repetidos e simultâneos, o bloqueio por data e
as falhas na escrita da cópia. Os testes cobrem também falhas na confirmação da
transação e a recuperação da cópia de output. `test_admin_printing.py` testa os
dois serviços com o protocolo XML-RPC real, incluindo a preservação do PDF após novas inscrições
e a atualização de oito para dezasseis páginas apenas após um novo pedido.
