"""Default email templates seeded on startup. Admin can edit via panel."""

from __future__ import annotations

from app.services.email.email_builder import build_email_layout

APP_NAME = "Chivapp"

EMAIL_TEMPLATE_DEFAULTS: list[dict] = [
    {
        "slug": "welcome",
        "name": "Bienvenida",
        "description": "Se envÃ­a al crear una cuenta con correo y contraseÃ±a.",
        "subject": "Â¡Bienvenido a Chivapp, {{user_name}}!",
        "available_variables": [
            "user_name",
            "user_email",
            "app_name",
            "login_url",
        ],
        "html_body": build_email_layout(
            category_badge="Bienvenida",
            badge_bg="#DCFCE7",
            badge_color="#15803D",
            title="Â¡Bienvenido a {{app_name}}!",
            greeting="Hola {{user_name}},",
            lead_text="Tu cuenta ha sido creada exitosamente con el correo <strong>{{user_email}}</strong>. Desde Chivapp puedes descubrir mÃºsicos, gestionar reservas y coordinar tus presentaciones.",
            info_items=[
                ("Correo registrado", "{{user_email}}"),
                ("Plataforma", "{{app_name}}"),
            ],
            cta_label="Ir a Chivapp",
            cta_url="{{login_url}}",
            secondary_note="Si no creaste esta cuenta, puedes ignorar este mensaje.",
        ),
        "text_body": """
Hola {{user_name}},

Â¡Bienvenido a {{app_name}}!

Tu cuenta quedÃ³ creada con el correo {{user_email}}.

Ingresa aquÃ­: {{login_url}}

Si no creaste esta cuenta, ignora este mensaje.
""".strip(),
    },
    {
        "slug": "email_verification",
        "name": "VerificaciÃ³n de correo",
        "description": "Enlace para confirmar el correo electrÃ³nico de la cuenta.",
        "subject": "Confirma tu correo en {{app_name}}",
        "available_variables": [
            "user_name",
            "user_email",
            "app_name",
            "action_url",
            "expires_hours",
        ],
        "html_body": build_email_layout(
            category_badge="Seguridad",
            badge_bg="#FEF3C7",
            badge_color="#92400E",
            title="Confirma tu correo electrÃ³nico",
            greeting="Hola {{user_name}},",
            lead_text="Para activar tu cuenta en {{app_name}} y mantenerla protegida, confirma que esta direcciÃ³n de correo te pertenece pulsando el siguiente botÃ³n:",
            cta_label="Verificar mi correo",
            cta_url="{{action_url}}",
            secondary_note="â³ Este enlace expira en {{expires_hours}} horas. Si no creaste una cuenta, puedes ignorar este mensaje.",
        ),
        "text_body": """
Hola {{user_name}},

Confirma tu correo en {{app_name}} visitando:
{{action_url}}

El enlace expira en {{expires_hours}} horas.
Si no creaste una cuenta, ignora este mensaje.
""".strip(),
    },
    {
        "slug": "password_reset",
        "name": "Restablecer contraseÃ±a",
        "description": "Enlace para crear una nueva contraseÃ±a.",
        "subject": "Restablece tu contraseÃ±a en {{app_name}}",
        "available_variables": [
            "user_name",
            "user_email",
            "app_name",
            "action_url",
            "expires_hours",
        ],
        "html_body": build_email_layout(
            category_badge="Seguridad",
            badge_bg="#FEE2E2",
            badge_color="#991B1B",
            title="Restablece tu contraseÃ±a",
            greeting="Hola {{user_name}},",
            lead_text="Recibimos una solicitud para restablecer la contraseÃ±a asociada a <strong>{{user_email}}</strong>. Haz clic en el botÃ³n para crear tu nueva contraseÃ±a:",
            cta_label="Crear nueva contraseÃ±a",
            cta_url="{{action_url}}",
            secondary_note="â³ El enlace expira en {{expires_hours}} horas. Si tÃº no solicitaste este cambio, no te preocupes; tu cuenta sigue segura y puedes ignorar este mensaje.",
        ),
        "text_body": """
Hola {{user_name}},

Restablece tu contraseÃ±a en {{app_name}}:
{{action_url}}

El enlace expira en {{expires_hours}} horas.
Si no solicitaste este cambio, ignora este mensaje.
""".strip(),
    },
    {
        "slug": "ensemble_invite",
        "name": "InvitaciÃ³n a integrante",
        "description": "Un lÃ­der invita a alguien a unirse a su agrupaciÃ³n.",
        "subject": "{{leader_name}} te invitÃ³ a unirte a su agrupaciÃ³n en {{app_name}}",
        "available_variables": [
            "member_name",
            "member_email",
            "leader_name",
            "app_name",
            "action_url",
            "expires_days",
            "specialties",
        ],
        "html_body": build_email_layout(
            category_badge="AgrupaciÃ³n",
            badge_bg="#EDE9FE",
            badge_color="#5B21B6",
            title="Te invitaron a una agrupaciÃ³n musical",
            greeting="Hola {{member_name}},",
            lead_text="<strong>{{leader_name}}</strong> te agregÃ³ como integrante en {{app_name}}. Crea tu contraseÃ±a para activar tu cuenta, ver convocatorias y coordinar tus presentaciones:",
            info_items=[
                ("LÃ­der / Grupo", "{{leader_name}}"),
                ("Especialidades", "{{specialties}}"),
            ],
            cta_label="Crear mi contraseÃ±a y unirme",
            cta_url="{{action_url}}",
            secondary_note="â³ El enlace de invitaciÃ³n expira en {{expires_days}} dÃ­as.",
        ),
        "text_body": """
Hola {{member_name}},

{{leader_name}} te invitÃ³ a unirte a su agrupaciÃ³n en {{app_name}}.
Especialidades: {{specialties}}

Crea tu contraseÃ±a aquÃ­:
{{action_url}}

El enlace expira en {{expires_days}} dÃ­as.
""".strip(),
    },
    {
        "slug": "booking_member_invite",
        "name": "Convocatoria a evento",
        "description": "Un lÃ­der convoca a un integrante para un evento confirmado.",
        "subject": "Convocatoria: {{leader_name}} te convocÃ³ a {{event_type}}",
        "available_variables": [
            "member_name",
            "leader_name",
            "app_name",
            "event_type",
            "event_date",
            "event_time",
            "event_location",
            "action_url",
        ],
        "html_body": build_email_layout(
            category_badge="Convocatoria",
            badge_bg="#FEF9C3",
            badge_color="#854D0E",
            title="Nueva convocatoria para evento",
            greeting="Hola {{member_name}},",
            lead_text="<strong>{{leader_name}}</strong> te ha convocado a una presentaciÃ³n en {{app_name}}. Revisa los datos y confirma tu asistencia:",
            info_items=[
                ("Tipo de evento", "{{event_type}}"),
                ("Fecha", "{{event_date}}"),
                ("Hora de inicio", "{{event_time}} (Hora PerÃº)"),
                ("UbicaciÃ³n", "{{event_location}}"),
            ],
            cta_label="Ver convocatoria y responder",
            cta_url="{{action_url}}",
            secondary_note="Por favor responde a la brevedad para que el lÃ­der pueda cerrar la formaciÃ³n del grupo.",
        ),
        "text_body": """
Hola {{member_name}},

{{leader_name}} te convocÃ³ a {{event_type}}.
Fecha: {{event_date}}
Hora: {{event_time}} (Hora PerÃº)
UbicaciÃ³n: {{event_location}}

Responde aquÃ­: {{action_url}}
""".strip(),
    },
    {
        "slug": "booking_new_request",
        "name": "Nueva solicitud de reserva",
        "description": "Se envÃ­a al mÃºsico cuando un cliente genera una solicitud de reserva.",
        "subject": "Â¡Nueva solicitud de {{contractor_name}} para {{event_type}} en {{app_name}}!",
        "available_variables": [
            "musician_name",
            "contractor_name",
            "app_name",
            "event_type",
            "event_date",
            "event_time",
            "event_location",
            "event_description",
            "action_url",
        ],
        "html_body": build_email_layout(
            category_badge="Nueva Solicitud",
            badge_bg="#E0F2FE",
            badge_color="#0369A1",
            title="Â¡Tienes una nueva solicitud de reserva!",
            greeting="Hola {{musician_name}},",
            lead_text="El cliente <strong>{{contractor_name}}</strong> estÃ¡ interesado en tu agrupaciÃ³n y te ha enviado una solicitud de reserva en {{app_name}}:",
            info_items=[
                ("Cliente", "{{contractor_name}}"),
                ("Tipo de evento", "{{event_type}}"),
                ("Fecha del evento", "{{event_date}}"),
                ("Hora de inicio", "{{event_time}} (Hora PerÃº)"),
                ("Lugar / DirecciÃ³n", "{{event_location}}"),
                ("Detalles / Mensaje", "{{event_description}}"),
            ],
            cta_label="Ver solicitud y cotizar",
            cta_url="{{action_url}}",
            secondary_note="Responde a tiempo para asegurar la reserva y brindar un excelente servicio.",
        ),
        "text_body": """
Hola {{musician_name}},

El cliente {{contractor_name}} te ha enviado una nueva solicitud de reserva en {{app_name}}:

Tipo de evento: {{event_type}}
Fecha: {{event_date}}
Hora: {{event_time}} (Hora PerÃº)
UbicaciÃ³n: {{event_location}}
Detalles: {{event_description}}

Revisa la solicitud y envÃ­a tu cotizaciÃ³n aquÃ­:
{{action_url}}
""".strip(),
    },
    {
        "slug": "profile_approved",
        "name": "Perfil verificado y aprobado",
        "description": "Se envÃ­a al usuario cuando su perfil de mÃºsico o contratista es aprobado por el administrador.",
        "subject": "Â¡Felicidades {{user_name}}! Tu perfil ha sido verificado en {{app_name}}",
        "available_variables": [
            "user_name",
            "role_label",
            "app_name",
            "action_url",
        ],
        "html_body": build_email_layout(
            category_badge="VerificaciÃ³n",
            badge_bg="#DCFCE7",
            badge_color="#15803D",
            title="Â¡Tu perfil ha sido verificado!",
            greeting="Hola {{user_name}},",
            lead_text="Nos alegra informarte que tu perfil de <strong>{{role_label}}</strong> ha sido revisado y verificado exitosamente por el equipo de {{app_name}}. Ya tienes acceso completo a todas las funciones de la plataforma.",
            info_items=[
                ("Estado", "Verificado y Aprobado"),
                ("Tipo de perfil", "{{role_label}}"),
                ("Plataforma", "{{app_name}}"),
            ],
            cta_label="Ir a mi panel",
            cta_url="{{action_url}}",
            secondary_note="Tu perfil ya es visible y estÃ¡ habilitado para gestionar reservas.",
        ),
        "text_body": """
Hola {{user_name}},

Nos alegra informarte que tu perfil de {{role_label}} ha sido revisado y verificado exitosamente por el equipo de {{app_name}}.

Ya tienes acceso completo a todas las funciones de la plataforma.

Puedes acceder a tu panel aquÃ­:
{{action_url}}
""".strip(),
    },
    {
        "slug": "profile_rejected",
        "name": "Perfil requiere correcciones",
        "description": "Se envÃ­a al usuario cuando su perfil requiere correcciones tras la revisiÃ³n del administrador.",
        "subject": "Tu perfil de {{role_label}} requiere correcciones en {{app_name}}",
        "available_variables": [
            "user_name",
            "role_label",
            "reason",
            "app_name",
            "action_url",
        ],
        "html_body": build_email_layout(
            category_badge="RevisiÃ³n de Perfil",
            badge_bg="#FEF3C7",
            badge_color="#92400E",
            title="Tu perfil requiere algunos ajustes",
            greeting="Hola {{user_name}},",
            lead_text="El equipo de {{app_name}} revisÃ³ tu perfil de <strong>{{role_label}}</strong> y solicita que realices las siguientes correcciones antes de poder verificarlo:",
            info_items=[
                ("Tipo de perfil", "{{role_label}}"),
                ("Motivo / Ajuste requerido", "{{reason}}"),
            ],
            cta_label="Editar y corregir mi perfil",
            cta_url="{{action_url}}",
            secondary_note="Una vez que apliques los cambios, vuelve a enviar tu perfil para completar la verificaciÃ³n.",
        ),
        "text_body": """
Hola {{user_name}},

El equipo de {{app_name}} revisÃ³ tu perfil de {{role_label}} y solicita que realices unas correcciones antes de poder verificarlo:

Motivo / Ajuste:
{{reason}}

Edita y corrige tu perfil aquÃ­:
{{action_url}}
""".strip(),
    },
    {
        "slug": "booking_quoted",
        "name": "CotizaciÃ³n recibida",
        "description": "Se envÃ­a al contratista cuando el mÃºsico responde su solicitud con una cotizaciÃ³n.",
        "subject": "Â¡{{musician_name}} te ha enviado una cotizaciÃ³n en {{app_name}}!",
        "available_variables": [
            "contractor_name",
            "musician_name",
            "app_name",
            "event_type",
            "price",
            "action_url",
        ],
        "html_body": build_email_layout(
            category_badge="CotizaciÃ³n Recibida",
            badge_bg="#E0F2FE",
            badge_color="#0369A1",
            title="Â¡Tienes una cotizaciÃ³n para tu evento!",
            greeting="Hola {{contractor_name}},",
            lead_text="<strong>{{musician_name}}</strong> ha respondido a tu solicitud para el evento <strong>{{event_type}}</strong> con una cotizaciÃ³n:",
            info_items=[
                ("MÃºsico / AgrupaciÃ³n", "{{musician_name}}"),
                ("Tipo de evento", "{{event_type}}"),
                ("Monto cotizado", "S/ {{price}}"),
            ],
            cta_label="Ver cotizaciÃ³n y confirmar",
            cta_url="{{action_url}}",
            secondary_note="Revisa la cotizaciÃ³n en Chivapp para aceptar el contrato y asegurar la fecha de tu evento.",
        ),
        "text_body": """
Hola {{contractor_name}},

{{musician_name}} ha respondido a tu solicitud para el evento {{event_type}} con una cotizaciÃ³n de S/ {{price}}.

Revisa la cotizaciÃ³n y confirma tu reserva aquÃ­:
{{action_url}}
""".strip(),
    },
    {
        "slug": "booking_review_reminder",
        "name": "Recordatorio de reseña del evento",
        "description": "Se envía 5 horas después de iniciar el evento para pedir una reseña.",
        "subject": "Tu evento ha finalizado, ¡déjanos tu reseña!",
        "available_variables": ["user_name", "other_party_name", "action_url"],
        "html_body": build_email_layout(
            category_badge="Reseña Pendiente",
            title="¿Cómo estuvo el evento?",
            greeting="Hola {{user_name}}",
            lead_text="Esperamos que tu evento haya sido un éxito. Ha llegado el momento de calificar la experiencia con {{other_party_name}}.",
            body_extra_html="<p>Las reseñas ayudan a mantener la confianza y seguridad en nuestra comunidad. ¡Te tomará menos de un minuto!</p>",
            cta_label="Dejar mi reseña",
            cta_url="{{action_url}}",
        ),
        "text_body": """
Hola {{user_name}},

Esperamos que el evento haya sido un éxito.
Por favor, tómate un minuto para calificar tu experiencia con {{other_party_name}}.

Déjanos tu reseña aquí:
{{action_url}}
""",
    },
    {
        "slug": "booking_confirmed_musician",
        "name": "Reserva confirmada (Músico)",
        "description": "Se envía al músico cuando el contratista realiza el pago y se confirma la reserva.",
        "subject": "¡Reserva confirmada! {{event_type}}",
        "available_variables": ["musician_name", "event_type", "app_name", "action_url"],
        "html_body": build_email_layout(
            category_badge="Confirmado",
            title="¡Tienes un nuevo evento confirmado!",
            greeting="Hola {{musician_name}}",
            lead_text="El contratista ha completado el pago total para tu evento de {{event_type}}. ¡La reserva está oficialmente confirmada!",
            body_extra_html="<p>Puedes revisar los detalles del evento, ubicación y horario desde tu panel.</p>",
            cta_label="Ver evento",
            cta_url="{{action_url}}",
        ),
        "text_body": """
Hola {{musician_name}},

El contratista ha completado el pago para tu evento de {{event_type}}. ¡La reserva está confirmada!

Revisa los detalles aquí:
{{action_url}}
""",
    },
    {
        "slug": "booking_confirmed_contractor",
        "name": "Reserva confirmada (Contratista)",
        "description": "Se envía al contratista después de realizar el pago total.",
        "subject": "Pago exitoso - Reserva confirmada",
        "available_variables": ["contractor_name", "event_type", "app_name", "action_url"],
        "html_body": build_email_layout(
            category_badge="Confirmado",
            title="¡Tu evento está asegurado!",
            greeting="Hola {{contractor_name}}",
            lead_text="Hemos recibido tu pago para el evento de {{event_type}} exitosamente.",
            body_extra_html="<p>Tu dinero está seguro. El músico ya fue notificado y puedes coordinar los últimos detalles desde el chat de tu reserva.</p>",
            cta_label="Ver mi reserva",
            cta_url="{{action_url}}",
        ),
        "text_body": """
Hola {{contractor_name}},

Hemos recibido tu pago para el evento de {{event_type}}. ¡La reserva está confirmada!

Revisa los detalles y coordina por el chat aquí:
{{action_url}}
""",
    },
    {
        "slug": "booking_member_accepted_leader",
        "name": "Integrante aceptó invitación al evento",
        "description": "Se envía al músico líder cuando un integrante acepta ir al evento.",
        "subject": "{{member_name}} confirmó su asistencia al evento",
        "available_variables": ["leader_name", "member_name", "event_type", "event_date", "event_time", "app_name"],
        "html_body": build_email_layout(
            category_badge="Equipo actualizado",
            title="Integrante confirmado",
            greeting="Hola {{leader_name}}",
            lead_text="{{member_name}} acaba de aceptar tu invitación para participar en el evento de {{event_type}}.",
            body_extra_html="<p><strong>Fecha:</strong> {{event_date}}<br><strong>Hora:</strong> {{event_time}}</p><p>El integrante ya tiene acceso a los detalles y al chat de coordinación.</p>",
        ),
        "text_body": """
Hola {{leader_name}},

{{member_name}} ha aceptado participar en tu evento de {{event_type}} el {{event_date}} a las {{event_time}}.
""",
    },
]

