---
doc_id: DOC-PRODUCT-OPS
title: ParcelPilot Product Operations Guide and Known Issues
source_file: 04_Product_Operations_Guide_and_Known_Issues.pdf
doc_type: product_doc
status: CURRENT
effective_date: 2026-08-14
authority_tier: 3
applies_to: ALL
---

# ParcelPilot Product Operations Guide

Status: CURRENT
Updated: 14 August 2026

## 1. Plan capabilities

- **Bulk Upload:** Available on Growth and Enterprise. Supported file size is up to 5,000 rows per CSV.
- **Standard:** Bulk Upload is not included.
- **Shipment status:** BOOKED means the shipment is created but ParcelPilot has not yet received a pickup
  confirmation. PICKED_UP means carrier pickup has been confirmed.

## 2. Current known issues

### KI-208 - Bulk Upload failures on large CSVs

Opened: 10 August 2026
Status: Investigating

Some Growth and Enterprise customers experience intermittent failures on CSV uploads above approximately 3,000
rows, even though the supported product limit remains 5,000 rows. Workaround: split the upload into files below
3,000 rows. Individual shipment creation is unaffected.

### KI-211 - SwiftShip pickup webhook delay

Opened: 12 August 2026
Status: Monitoring

SwiftShip pickup confirmation webhooks can arrive up to 20 minutes late. A parcel may physically be collected while
ParcelPilot still shows BOOKED. Before telling a customer that a pickup did not occur, verify the carrier status or wait
through the known delay window.

## 3. Resolved issue

**KI-176 - Address validation:** Resolved 18 July 2026. Do not use this resolved issue to explain new incidents unless
evidence specifically matches it.
