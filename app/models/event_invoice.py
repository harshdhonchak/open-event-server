import logging
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from flask.templating import render_template
from sqlalchemy.sql import func

from app.api.helpers.db import get_new_identifier
from app.api.helpers.files import create_save_pdf
from app.api.helpers.mail import (
    send_email_for_monthly_fee_payment,
    send_followup_email_for_monthly_fee_payment,
)
from app.api.helpers.notification import (
    send_followup_notif_monthly_fee_payment,
    send_notif_monthly_fee_payment,
)
from app.api.helpers.storage import UPLOAD_PATHS
from app.api.helpers.utilities import monthdelta
from app.models import db
from app.models.base import SoftDeletionModel
from app.models.order import Order
from app.models.setting import Setting
from app.models.ticket_fee import TicketFees
from app.settings import get_settings

logger = logging.getLogger(__name__)


def get_new_id():
    return get_new_identifier(EventInvoice, length=8)


def round_money(money):
    return Decimal(money).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class EventInvoice(SoftDeletionModel):
    DUE_DATE_DAYS = 30

    __tablename__ = 'event_invoices'

    id = db.Column(db.Integer, primary_key=True)
    identifier = db.Column(db.String, unique=True, default=get_new_id)
    amount = db.Column(db.Float)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    event_id = db.Column(db.Integer, db.ForeignKey('events.id', ondelete='SET NULL'))

    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    # Payment Fields
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True, default=None)
    transaction_id = db.Column(db.String)
    paid_via = db.Column(db.String)
    payment_mode = db.Column(db.String)
    brand = db.Column(db.String)
    exp_month = db.Column(db.Integer)
    exp_year = db.Column(db.Integer)
    last4 = db.Column(db.String)
    stripe_token = db.Column(db.String)
    paypal_token = db.Column(db.String)
    status = db.Column(db.String, default='due')

    invoice_pdf_url = db.Column(db.String)

    event = db.relationship('Event', backref='invoices')
    user = db.relationship('User', backref='event_invoices')

    def __init__(self, **kwargs):
        super(EventInvoice, self).__init__(**kwargs)

        if not self.created_at:
            self.created_at = datetime.utcnow()

        if not self.identifier:
            self.identifier = self.created_at.strftime('%Y%mU-') + get_new_id()

    def __repr__(self):
        return '<EventInvoice %r>' % self.invoice_pdf_url

    @property
    def due_at(self):
        return self.created_at + timedelta(days=EventInvoice.DUE_DATE_DAYS)

    def populate(self):
        assert self.event is not None

        with db.session.no_autoflush:
            self.user = self.event.owner
            return self.generate_pdf()

    def generate_pdf(self):
        with db.session.no_autoflush:
            latest_invoice_date = (
                EventInvoice.query.filter_by(event=self.event)
                .filter(EventInvoice.created_at < self.created_at)
                .with_entities(func.max(EventInvoice.created_at))
                .scalar()
            )

            admin_info = Setting.query.first()
            currency = self.event.payment_currency
            ticket_fee_object = (
                TicketFees.query.filter_by(country=self.event.payment_country).first()
                or TicketFees.query.filter_by(country='global').first()
            )
            if not ticket_fee_object:
                logger.error(
                    'Ticket Fee not found for event id {id}'.format(id=self.event.id)
                )
                return

            ticket_fee_percentage = ticket_fee_object.service_fee
            ticket_fee_maximum = ticket_fee_object.maximum_fee
            gross_revenue = self.event.calc_revenue(
                start=latest_invoice_date, end=self.created_at
            )
            invoice_amount = gross_revenue * (ticket_fee_percentage / 100)
            if invoice_amount > ticket_fee_maximum:
                invoice_amount = ticket_fee_maximum
            self.amount = round_money(invoice_amount)
            net_revenue = round_money(gross_revenue - invoice_amount)
            orders_query = self.event.get_orders_query(
                start=latest_invoice_date, end=self.created_at
            )
            first_order_date = orders_query.with_entities(
                func.min(Order.completed_at)
            ).scalar()
            last_order_date = orders_query.with_entities(
                func.max(Order.completed_at)
            ).scalar()
            payment_details = {
                'tickets_sold': self.event.tickets_sold,
                'gross_revenue': round_money(gross_revenue),
                'net_revenue': round_money(net_revenue),
                'first_date': first_order_date,
                'last_date': last_order_date,
            }
            self.invoice_pdf_url = create_save_pdf(
                render_template(
                    'pdf/event_invoice.html',
                    user=self.user,
                    admin_info=admin_info,
                    currency=currency,
                    event=self.event,
                    ticket_fee_object=ticket_fee_object,
                    payment_details=payment_details,
                    net_revenue=net_revenue,
                    invoice=self,
                ),
                UPLOAD_PATHS['pdf']['event_invoice'],
                dir_path='/static/uploads/pdf/event_invoices/',
                identifier=self.identifier,
                extra_identifiers={'event_identifier': self.event.identifier},
                new_renderer=True,
            )

        return self.invoice_pdf_url

    def send_notification(self, follow_up=False):
        prev_month = monthdelta(self.created_at, 1).strftime(
            "%b %Y"
        )  # Displayed as Aug 2016
        app_name = get_settings()['app_name']
        frontend_url = get_settings()['frontend_url']
        link = '{}/event-invoice/{}/review'.format(frontend_url, self.identifier)
        email_function = send_email_for_monthly_fee_payment
        notification_function = send_notif_monthly_fee_payment
        if follow_up:
            email_function = send_followup_email_for_monthly_fee_payment
            notification_function = send_followup_notif_monthly_fee_payment
        email_function(
            self.user.email, self.event.name, prev_month, self.amount, app_name, link,
        )
        notification_function(
            self.user,
            self.event.name,
            prev_month,
            self.amount,
            app_name,
            link,
            self.event_id,
        )
